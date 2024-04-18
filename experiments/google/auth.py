import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


class AuthManager:
    def __init__(self, client_secrets_file, scopes, user_id=None):
        self.client_secrets_file = client_secrets_file
        self.scopes = scopes
        self.user_id = user_id  # Optional user ID to handle multiple users.

    def format_user_id(self, user_id):
        # Convert the email address into a filesystem-friendly format.
        if user_id:
            return user_id.replace("@", "_at_").replace(".", "_dot_")
        return user_id

    def authenticate_user(self):
        creds = self.load_credentials()
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = self.run_authentication_flow()
        self.save_credentials(creds)
        return creds

    def get_credentials_filename(self):
        # Generate a credentials file name based on the formatted user_id (email).
        filename = "token"
        if self.user_id:
            filename += f"_{self.user_id}"
        return f"{filename}.json"

    def load_credentials(self):
        # Method implementation remains the same.
        creds = None
        credentials_path = self.get_credentials_filename()
        if os.path.exists(credentials_path):
            creds = Credentials.from_authorized_user_file(credentials_path, self.scopes)
        return creds

    def save_credentials(self, creds):
        with open(self.get_credentials_filename(), "w") as token_file:
            token_file.write(creds.to_json())

    def run_authentication_flow(self):
        flow = InstalledAppFlow.from_client_secrets_file(
            self.client_secrets_file, self.scopes
        )
        creds = flow.run_local_server(port=0)
        return creds


class GoogleAPI:
    def __init__(self, service_name, service_version, scopes, user_id=None):
        self.auth_manager = AuthManager("credentials.json", scopes, user_id=user_id)
        creds = self.auth_manager.authenticate_user()
        self.service = build(service_name, service_version, credentials=creds)
