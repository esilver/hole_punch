import os
import requests
from flask import Flask

app = Flask(__name__)

IP_ECHO_SERVICE_URL = "https://api.ipify.org" # A simple service that returns the requestor's IP

@app.route('/')
def get_my_public_ip():
    try:
        print(f"Cloud Run service 'ip-worker-service' making request to: {IP_ECHO_SERVICE_URL}")
        response = requests.get(IP_ECHO_SERVICE_URL, timeout=10)
        response.raise_for_status()
        
        public_ip = response.text.strip()
        
        log_message = f"The IP echo service reported this service's public IP as: {public_ip}"
        print(log_message)
        
        verification_note = (
            "VERIFICATION STEP: In the Google Cloud Console, navigate to 'VPC Network' -> 'Cloud NAT'. "
            "Select the 'ip-worker-nat' gateway in the 'us-central1' region. "
            "Confirm that the IP address above is listed under 'NAT IP addresses' for that gateway."
        )
        print(verification_note)
        
        formatted_verification_note = verification_note.replace('\n', '<br>')
        return f"{log_message}<br><br>{formatted_verification_note}", 200

    except requests.exceptions.RequestException as e:
        error_message = f"Error connecting to IP echo service: {e}"
        print(error_message)
        return error_message, 500

if __name__ == "__main__":
    # This section is primarily for local testing and not directly used by Gunicorn on Cloud Run
    # Gunicorn is started by the CMD in the Dockerfile
    port = int(os.environ.get("PORT", 8080))
    # The app.run() below is not strictly necessary if only deploying to Cloud Run via Gunicorn.
    # However, it allows local execution for testing if needed: `python main.py`
    # For Cloud Run, Gunicorn uses the 'app' Flask object.
    if os.environ.get("K_SERVICE"): # K_SERVICE is set by Cloud Run
        pass # Gunicorn will serve 'app'
    else: # Local execution
        app.run(host='0.0.0.0', port=port, debug=True) 