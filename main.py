from flask import Flask, request
import requests
from bs4 import BeautifulSoup
import json
import base64
import re 
import os
from urllib.parse import urlparse, urljoin

app = Flask(__name__)

VERIFY_TOKEN = "hello"

class WhatsAppClient:
    def __init__(self, phone_number_id, access_token):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.api_url = f"https://graph.facebook.com/v17.0/{phone_number_id}/messages"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

    def send_message(self, to, text):
        """Send a text message"""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": text}
        }

        return requests.post(
            self.api_url,
            headers=self.headers,
            data=json.dumps(payload)
        )

    def send_document(self, to, document_content, filename, caption=None):
        """Send a document"""
        # First, we need to upload the document to Meta's servers
        upload_url = f"https://graph.facebook.com/v17.0/{self.phone_number_id}/media"

        files = {
            'file': (filename, document_content, 'application/json')
        }

        upload_response = requests.post(
            upload_url,
            headers={"Authorization": f"Bearer {self.access_token}"},
            files=files
        )

        if upload_response.status_code != 200:
            raise Exception(f"Failed to upload document: {upload_response.text}")

        media_id = upload_response.json()['id']

        # Now send the document message
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "document",
            "document": {
                "id": media_id,
                "filename": filename
            }
        }

        if caption:
            payload["document"]["caption"] = caption

        return requests.post(
            self.api_url,
            headers=self.headers,
            data=json.dumps(payload)
        )

class WebProxy:
    def __init__(self):
        self.whatsapp = WhatsAppClient(
            phone_number_id=os.getenv('PHONE_NUMBER_ID'),
            access_token=os.getenv('WHATSAPP_TOKEN')
        )
        self.user_sessions = {}
        self.chunk_size = 1000 * 1024  # 1000KB chunks

    def process_webpage(self, url):
        """Fetch and process webpage into transportable chunks"""
        try:
            response = requests.get(url, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')

            # Remove scripts and styles
            for script in soup(["script", "style"]):
                script.decompose()

            # Process images - convert to base64 or placeholders
            for img in soup.find_all('img'):
                try:
                    img_url = urljoin(url, img['src'])
                    img_response = requests.get(img_url)
                    if img_response.status_code == 200:
                        img_base64 = base64.b64encode(img_response.content).decode()
                        img['src'] = f"data:{img_response.headers['Content-Type']};base64,{img_base64}"
                except:
                    img['src'] = '#'  # Placeholder for failed images

            # Convert to transportable format
            page_data = {
                'url': url,
                'title': soup.title.string if soup.title else 'Untitled',
                'content': str(soup),
                'timestamp': str(datetime.now())
            }

            # Chunk the data
            serialized = json.dumps(page_data)
            chunks = [serialized[i:i + self.chunk_size] 
                     for i in range(0, len(serialized), self.chunk_size)]

            return chunks

        except Exception as e:
            return [json.dumps({'error': str(e)})]

    def handle_command(self, user_id, command):
        """Process user commands"""
        if command.startswith('fetch:'):
            url = command[6:].strip()
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url

            chunks = self.process_webpage(url)

            # Send chunks via WhatsApp
            for i, chunk in enumerate(chunks):
                metadata = {
                    'chunk_index': i,
                    'total_chunks': len(chunks),
                    'url': url
                }

                # Prepare message for WhatsApp
                message = json.dumps({
                    'metadata': metadata,
                    'data': chunk
                })

                # Send as document to preserve formatting
                self.whatsapp.send_document(
                    to=user_id,
                    document_content=message.encode(),
                    filename=f'webpage_chunk_{i}.json',
                    caption=f'Chunk {i+1} of {len(chunks)}'
                )

            return f"Sent webpage in {len(chunks)} chunks. Use your browser app to view."

        return "Unknown command. Use 'fetch:URL' to request a webpage."

# Add this new route for webhook verification
@app.route("/webhook", methods=['GET'])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("WEBHOOK_VERIFIED")
            return challenge
        else:
            return "Forbidden", 403

    return "Not Found", 404

@app.route("/webhook", methods=['POST'])
def webhook():
    """Handle incoming WhatsApp messages"""
    data = request.get_json()
    
    # Extract message and user info from WhatsApp webhook
    try:
        message = data['entry'][0]['changes'][0]['value']['messages'][0]
        user_id = message['from']
        
        if 'text' in message:
            command = message['text']['body']
            proxy = WebProxy()
            response = proxy.handle_command(user_id, command)
            
            # Send text response
            proxy.whatsapp.send_message(
                to=user_id,
                text=response
            )
            
    except Exception as e:
        print(f"Error processing webhook: {e}")
    
    return "OK"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)
