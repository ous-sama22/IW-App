from flask import Flask, request, Response
import os
import requests
from bs4 import BeautifulSoup
import time
from dotenv import load_dotenv
import json
import redis
from datetime import datetime
import zipfile
import re
import logging
from PIL import Image
import io

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Initialize Redis
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

class WhatsAppFileSender:
    def __init__(self):
        self.whatsapp_token = os.getenv('WHATSAPP_TOKEN')
        self.phone_number_id = os.getenv('PHONE_NUMBER_ID')
        self.api_url = f"https://graph.facebook.com/v17.0/{self.phone_number_id}/messages"
        self.headers = {
            "Authorization": f"Bearer {self.whatsapp_token}",
            "Content-Type": "application/json"
        }

    def get_user_message_count(self, user_number):
        count = redis_client.get(f"whatsapp_count:{user_number}")
        return int(count) if count else 0

    def increment_user_message_count(self, user_number):
        key = f"whatsapp_count:{user_number}"
        redis_client.incr(key)
        redis_client.expire(key, 24 * 60 * 60)

    def can_send_media(self, user_number):
        return self.get_user_message_count(user_number) < 12

    def send_text(self, recipient, message):
        try:
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "text",
                "text": {"body": message}
            }
            
            response = requests.post(
                self.api_url,
                headers=self.headers,
                json=payload
            )
            logger.debug(f"Text message response: {response.text}")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Error sending text message: {str(e)}")
            return False

    def send_document(self, recipient, file_path, caption=""):
        ALLOWED_EXTENSIONS = {'.pdf'}
        if not any(file_path.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS):
            raise Exception("Invalid file type")
        if not self.can_send_media(recipient):
            return False, "Daily media message limit reached (12/24hrs)"

        try:
            # Check if file exists and size
            if not os.path.exists(file_path):
                raise Exception(f"File not found: {file_path}")
            
            file_size = os.path.getsize(file_path)
            if file_size > 100 * 1024 * 1024:  # 100MB limit
                raise Exception("File size exceeds WhatsApp's 100MB limit")

            logger.debug(f"Uploading file: {file_path} ({file_size} bytes)")
            
            # Open file in binary mode
            with open(file_path, 'rb') as file:
                # Send as application/octet-stream which is generally accepted
                files = {
                    'file': (os.path.basename(file_path), file, 'application/pdf')
                }
                data = {
                    'messaging_product': 'whatsapp'
                }
                
                upload_response = requests.post(
                    f"https://graph.facebook.com/v17.0/{self.phone_number_id}/media",
                    headers={"Authorization": f"Bearer {self.whatsapp_token}"},
                    data=data,
                    files=files
                )

            logger.debug(f"Upload response: {upload_response.text}")
            
            if upload_response.status_code != 200:
                raise Exception(f"Failed to upload document: {upload_response.text}")

            media_id = upload_response.json().get("id")
            if not media_id:
                raise Exception("No media ID received in upload response")

            # Send document
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "document",
                "document": {
                    "id": media_id,
                    "caption": caption
                }
            }

            response = requests.post(
                self.api_url,
                headers=self.headers,
                json=payload
            )
            
            logger.debug(f"Send document response: {response.text}")

            if response.status_code == 200:
                self.increment_user_message_count(recipient)
                return True, "Success"
            return False, f"Failed to send: {response.text}"

        except Exception as e:
            logger.error(f"Error sending document: {str(e)}")
            return False, str(e)

def is_processed_message(message_id):
    """Check if we've already processed this message"""
    return redis_client.get(f"processed_message:{message_id}") is not None

def mark_message_processed(message_id):
    """Mark message as processed with 1-hour expiry"""
    redis_client.setex(f"processed_message:{message_id}", 3600, "1")

def cleanup_temp_files(directory="temp_manga", max_age_hours=1):
    current_time = time.time()
    for filename in os.listdir(directory):
        filepath = os.path.join(directory, filename)
        file_age = current_time - os.path.getmtime(filepath)
        if file_age > max_age_hours * 3600:
            os.remove(filepath)

def process_manga_chapter(url, sender_number):
    logger.info(f"Processing manga chapter: {url} for {sender_number}")
    sender = WhatsAppFileSender()
    
    if not sender.can_send_media(sender_number):
        sender.send_text(
            sender_number, 
            "You've reached your daily limit of 12 media messages. Please try again after 24 hours."
        )
        return
    
    try:
        # Extract manga title and chapter number
        url_parts = url.strip('/').split('/')
        manga_title = url_parts[-2].replace('-', ' ').title()
        chapter_num = url_parts[-1]
        
        logger.info(f"Starting download for {manga_title} Chapter {chapter_num}")
        sender.send_text(sender_number, f"Starting to process {manga_title} Chapter {chapter_num}")
        
        # Create temp directory if it doesn't exist
        os.makedirs("temp_manga", exist_ok=True)
        
        # Fetch and process images
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        images = soup.find_all("img", class_="wp-manga-chapter-img")
        
        if not images:
            sender.send_text(sender_number, "No images found in this chapter.")
            return
            
        # Download images
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        
        images_data = []
        total_images = len(images)
        
        for idx, img in enumerate(images, 1):
            img_url = img['src']
            logger.debug(f"Downloading image {idx}/{total_images}: {img_url}")
            
            response = session.get(img_url)
            if response.status_code == 200:
                images_data.append((response.content, '.jpg'))
            time.sleep(1)
        
        # Save as PDF instead of CBZ
        pdf_filename = os.path.join("temp_manga", f"{manga_title}_Chapter_{chapter_num}.pdf")
        
        # Convert images to PDF

        images_pil = []
        first_image = None
        
        for idx, (image_data, _) in enumerate(images_data):
            img = Image.open(io.BytesIO(image_data))
            if img.mode == 'RGBA':
                img = img.convert('RGB')
            if idx == 0:
                first_image = img
            else:
                images_pil.append(img)
        
        if first_image:
            first_image.save(pdf_filename, "PDF", save_all=True, append_images=images_pil)
        
        logger.info(f"Created PDF file: {pdf_filename}")
        
        # Send PDF file
        success, message = sender.send_document(
            sender_number, 
            pdf_filename,
            f"{manga_title} - Chapter {chapter_num}"
        )
        
        if success:
            sender.send_text(
                sender_number,
                f"Successfully sent {manga_title} Chapter {chapter_num} as PDF file.\n"
                f"You have {12 - sender.get_user_message_count(sender_number)} media messages remaining today."
            )
        else:
            sender.send_text(sender_number, f"Failed to send PDF file: {message}")
        
        # Cleanup
        if os.path.exists(pdf_filename):
            os.remove(pdf_filename)
        
    except Exception as e:
        logger.error(f"Error in process_manga_chapter: {str(e)}")
        sender.send_text(sender_number, f"Error processing chapter: {str(e)}")
    finally:
        # Clean up temp directory if empty
        try:
            os.rmdir("temp_manga")
        except:
            pass

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        logger.debug(f"Received webhook data: {json.dumps(data, indent=2)}")
        
        # Check if this is a valid WhatsApp message webhook
        if not data.get('entry', []):
            logger.warning("No entry in webhook data")
            return Response(status=200)
            
        entry = data['entry'][0]
        if not entry.get('changes', []):
            logger.warning("No changes in webhook entry")
            return Response(status=200)
            
        change = entry['changes'][0]
        value = change.get('value', {})
        
        if not value.get('messages', []):
            logger.warning("No messages in webhook value")
            return Response(status=200)
            
        message = value['messages'][0]
        message_id = message.get('id')
        
        # Check if we've already processed this message
        if message_id and is_processed_message(message_id):
            logger.info(f"Message {message_id} already processed, skipping")
            return Response(status=200)
        
        sender = message.get('from')
        if message.get('type') == 'text':
            text = message['text']['body']
            
            if 'lekmanga.net/manga/' in text:
                # Mark message as processed before starting
                if message_id:
                    mark_message_processed(message_id)
                process_manga_chapter(text, sender)
            else:
                WhatsAppFileSender().send_text(
                    sender,
                    "Please send a valid lekmanga.net manga chapter URL."
                )
        
        return Response(status=200)
        
    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
        return Response(status=500)

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    logger.debug(f"Webhook verification request: mode={mode}, token={token}, challenge={challenge}")
    
    if mode and token:
        if mode == 'subscribe' and token == os.getenv('VERIFY_TOKEN'):
            return challenge
    
    return Response(status=403)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
