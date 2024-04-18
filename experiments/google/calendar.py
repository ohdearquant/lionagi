from googleapiclient.errors import HttpError

from .auth import GoogleAPI


class CalendarClient(GoogleAPI):
    def __init__(self):
        super().__init__("calendar", "v3", ["https://www.googleapis.com/auth/calendar"])

    def create_event(
        self,
        summary,
        location,
        description,
        start_time,
        end_time,
        attendees=None,
        reminders=None,
    ):
        event = {
            "summary": summary,
            "location": location,
            "description": description,
            "start": {"dateTime": start_time, "timeZone": "UTC"},
            "end": {"dateTime": end_time, "timeZone": "UTC"},
            "attendees": [{"email": email} for email in attendees] if attendees else [],
            "reminders": (
                reminders
                if reminders
                else {
                    "useDefault": False,
                    "overrides": [
                        {"method": "email", "minutes": 24 * 60},
                        {"method": "popup", "minutes": 10},
                    ],
                }
            ),
        }
        try:
            created_event = (
                self.service.events().insert(calendarId="primary", body=event).execute()
            )
            return created_event
        except HttpError as error:
            print(f"An error occurred: {error}")
            return None

    def cancel_event(self, event_id):
        self.service.events().delete(calendarId="primary", eventId=event_id).execute()

    def add_attendees(self, event_id, attendees):
        updated_event = (
            self.service.events().get(calendarId="primary", eventId=event_id).execute()
        )
        for attendee in attendees:
            updated_event["attendees"].append({"email": attendee})
        updated_event = (
            self.service.events()
            .update(calendarId="primary", eventId=event_id, body=updated_event)
            .execute()
        )
        return updated_event

    def send_event_invites(self, event_id):
        event = (
            self.service.events().get(calendarId="primary", eventId=event_id).execute()
        )
        event["sendUpdates"] = "all"
        updated_event = (
            self.service.events()
            .update(calendarId="primary", eventId=event_id, body=event)
            .execute()
        )
        return updated_event
