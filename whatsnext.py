#!/usr/bin/env python3
"""What's Next — macOS menu bar app showing countdown to your next Google Calendar meeting."""

import rumps
import requests
import keyring
import webbrowser
import hashlib
import base64
import os
import re
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta

from AppKit import (NSAttributedString, NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
                     NSColor, NSImage, NSApplication)
from Foundation import NSDictionary, NSSize

# Google OAuth credentials — load from environment variables
CLIENT_ID = os.environ.get("WHATSNEXT_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("WHATSNEXT_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:8777/callback"
SCOPES = "https://www.googleapis.com/auth/calendar.readonly"
KEYRING_SERVICE = "whatsnext"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth callback on localhost."""

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params:
            self.server.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Signed in! You can close this tab.</h2></body></html>")
        else:
            self.server.auth_code = None
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Sign in failed.</h2></body></html>")

    def log_message(self, format, *args):
        pass  # Silence request logs


def generate_pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def extract_meeting_link(event):
    """Extract meeting URL from a Google Calendar event."""
    # 1. Conference data entry points
    conference = event.get("conferenceData", {})
    for ep in conference.get("entryPoints", []):
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]

    # 2. Hangout link
    if event.get("hangoutLink"):
        return event["hangoutLink"]

    # 3. Search location and description for meeting URLs
    patterns = [
        r"https://meet\.google\.com/[a-z\-]+",
        r"https://[a-z0-9]*\.?zoom\.us/j/[0-9]+[^\s]*",
        r"https://teams\.microsoft\.com/l/meetup-join/[^\s]+",
        r"https://[a-z0-9]*\.?webex\.com/[^\s]+",
    ]
    for field in [event.get("location", ""), event.get("description", "")]:
        for pattern in patterns:
            match = re.search(pattern, field, re.IGNORECASE)
            if match:
                return match.group(0)

    return None


def get_icon_path():
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), '..', 'Resources', 'icon.png')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.png')


class WhatsNextApp(rumps.App):
    def __init__(self):
        icon_path = get_icon_path()
        super().__init__("", icon=icon_path, quit_button=None, template=True)
        self.current_event = None
        self.meeting_link = None
        self.access_token = None

        # Try to load saved refresh token
        refresh_token = keyring.get_password(KEYRING_SERVICE, "refresh_token")
        if refresh_token:
            self._refresh_access_token(refresh_token)

        # Build menu
        self._join_item = rumps.MenuItem("Join Meeting", callback=self._on_join)
        self._sign_in_item = rumps.MenuItem("Sign In to Google", callback=self._on_sign_in)
        self._sign_out_item = rumps.MenuItem("Sign Out", callback=self._on_sign_out)
        self.menu = [
            self._join_item,
            rumps.MenuItem("Refresh", callback=self._on_refresh),
            None,  # separator
            self._sign_in_item,
            self._sign_out_item,
            None,
            rumps.MenuItem("Quit", callback=self._on_quit),
        ]
        self._update_auth_menu()

        # Start timers
        self._fetch_timer = rumps.Timer(self._fetch_events, 60)
        self._display_timer = rumps.Timer(self._update_display, 15)
        self._fetch_timer.start()
        self._display_timer.start()

        # Initial fetch
        self._fetch_events(None)

    def _update_auth_menu(self):
        signed_in = self.access_token is not None
        self._sign_in_item.set_callback(None if signed_in else self._on_sign_in)
        self._sign_out_item.set_callback(self._on_sign_out if signed_in else None)

    def _on_join(self, _):
        if self.meeting_link:
            webbrowser.open(self.meeting_link)

    def _on_refresh(self, _):
        self._fetch_events(None)

    def _on_sign_in(self, _):
        threading.Thread(target=self._do_oauth, daemon=True).start()

    def _on_sign_out(self, _):
        self.access_token = None
        try:
            keyring.delete_password(KEYRING_SERVICE, "refresh_token")
        except keyring.errors.PasswordDeleteError:
            pass
        self.current_event = None
        self.meeting_link = None
        self._set_title("What's Next?")
        self._update_auth_menu()

    def _on_quit(self, _):
        rumps.quit_application()

    def _do_oauth(self):
        verifier, challenge = generate_pkce()

        # Start local server for callback
        server = HTTPServer(("localhost", 8777), OAuthCallbackHandler)
        server.auth_code = None
        server.timeout = 120

        # Open browser for Google sign-in
        auth_url = (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            f"client_id={CLIENT_ID}&"
            f"redirect_uri={REDIRECT_URI}&"
            "response_type=code&"
            f"scope={SCOPES}&"
            f"code_challenge={challenge}&"
            "code_challenge_method=S256&"
            "access_type=offline&"
            "prompt=consent"
        )
        webbrowser.open(auth_url)

        # Wait for callback
        server.handle_request()

        if server.auth_code:
            self._exchange_code(server.auth_code, verifier)
            self._update_auth_menu()
            self._fetch_events(None)

    def _exchange_code(self, code, verifier):
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
            },
        )
        tokens = resp.json()
        self.access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if refresh_token:
            keyring.set_password(KEYRING_SERVICE, "refresh_token", refresh_token)

    def _refresh_access_token(self, refresh_token):
        try:
            resp = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            tokens = resp.json()
            self.access_token = tokens.get("access_token")
        except Exception:
            self.access_token = None

    def _fetch_events(self, _):
        if not self.access_token:
            # Try refreshing
            refresh_token = keyring.get_password(KEYRING_SERVICE, "refresh_token")
            if refresh_token:
                self._refresh_access_token(refresh_token)
            if not self.access_token:
                self._set_title("What's Next?")
                return

        try:
            now = datetime.now(timezone.utc)
            time_max = now + timedelta(hours=24)

            resp = requests.get(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                headers={"Authorization": f"Bearer {self.access_token}"},
                params={
                    "timeMin": now.isoformat(),
                    "timeMax": time_max.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": "10",
                },
            )

            if resp.status_code == 401:
                # Token expired
                refresh_token = keyring.get_password(KEYRING_SERVICE, "refresh_token")
                if refresh_token:
                    self._refresh_access_token(refresh_token)
                    self._fetch_events(None)
                return

            data = resp.json()
            events = data.get("items", [])

            for event in events:
                # Skip all-day events
                start = event.get("start", {})
                if "dateTime" not in start:
                    continue

                # Skip cancelled
                if event.get("status") == "cancelled":
                    continue

                start_dt = datetime.fromisoformat(start["dateTime"])
                end_dt = datetime.fromisoformat(event["end"]["dateTime"])

                # Make timezone-aware if needed
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)

                # Skip ended events
                if end_dt <= now:
                    continue

                self.current_event = {
                    "title": event.get("summary", "Untitled"),
                    "start": start_dt,
                    "end": end_dt,
                }
                self.meeting_link = extract_meeting_link(event)
                self._update_display(None)
                return

            # No upcoming events
            self.current_event = None
            self.meeting_link = None
            self._update_display(None)

        except Exception as e:
            self._set_title("Error")

    def _set_title(self, text, color=None):
        """Set menu bar title with optional color, and tint the icon to match."""
        try:
            nsstatusitem = NSApplication.sharedApplication().delegate().nsstatusitem
            button = nsstatusitem.button()

            # Set text
            font = NSFont.menuBarFontOfSize_(0)
            attrs = {NSFontAttributeName: font}
            if color:
                attrs[NSForegroundColorAttributeName] = color
            attributed = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            if button:
                button.setAttributedTitle_(attributed)
            else:
                nsstatusitem.setAttributedTitle_(attributed)

            # Tint the icon
            if button:
                icon_path = get_icon_path()
                original = NSImage.alloc().initByReferencingFile_(icon_path)
                if color:
                    # Create a tinted copy
                    tinted = NSImage.alloc().initWithSize_(original.size())
                    tinted.lockFocus()
                    original.drawAtPoint_fromRect_operation_fraction_(
                        (0, 0), ((0, 0), original.size()), 1, 1)  # NSCompositeSourceOver
                    color.set()
                    from AppKit import NSRectFillUsingOperation
                    NSRectFillUsingOperation(((0, 0), original.size()), 3)  # NSCompositeSourceIn
                    tinted.unlockFocus()
                    tinted.setTemplate_(False)
                    nsstatusitem.setImage_(tinted)
                else:
                    original.setTemplate_(True)
                    nsstatusitem.setImage_(original)
        except Exception as e:
            # Falls back to plain title (e.g. during init before NSApp exists)
            rumps.App.title.fset(self, text)

    def _update_display(self, _):
        if not self.current_event:
            self._set_title("No meetings" if self.access_token else "What's Next?")
            return

        now = datetime.now(timezone.utc)
        start = self.current_event["start"]
        end = self.current_event["end"]
        title = self.current_event["title"]

        secs_until_start = (start - now).total_seconds()
        secs_until_end = (end - now).total_seconds()

        # Event ended
        if secs_until_end <= 0:
            self.current_event = None
            self.meeting_link = None
            self._set_title("No meetings" if self.access_token else "What's Next?")
            self._fetch_events(None)
            return

        # Determine color: green 2min before to 1min after, red after 1min past
        if secs_until_start <= 120 and secs_until_start > -60:
            color = NSColor.systemGreenColor()
        elif secs_until_start <= -60:
            color = NSColor.systemRedColor()
        else:
            color = None

        # Build display text
        if secs_until_start > 0:
            minutes = int(secs_until_start // 60) + 1
            if minutes >= 60:
                hours = minutes // 60
                remaining = minutes % 60
                if remaining == 0:
                    text = f"{hours} hr until {title}"
                else:
                    text = f"{hours} hr {remaining} min until {title}"
            else:
                text = f"{minutes} min until {title}"
        else:
            mins_left = int(secs_until_end // 60) + 1
            if mins_left >= 60:
                hours = mins_left // 60
                remaining = mins_left % 60
                if remaining == 0:
                    text = f"{title} — {hours} hr left"
                else:
                    text = f"{title} — {hours} hr {remaining} min left"
            else:
                text = f"{title} — {mins_left} min left"

        self._join_item.set_callback(self._on_join if self.meeting_link else None)
        self._set_title(text, color)


if __name__ == "__main__":
    WhatsNextApp().run()
