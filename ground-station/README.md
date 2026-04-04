# FinalPing Ground Station

Connects your local ADS-B receiver to your FinalPing account.
Gives you real ground data — actual landings, takeoffs, and taxi movement
that the cloud tracker can't detect.

## What you need

- A Raspberry Pi (any model) or a Windows/Mac/Linux computer
- An RTL-SDR dongle (~$30) or FlightAware Pro Stick (~$20)
- A 1090MHz antenna (~$20-40)
- dump1090 or PiAware installed and running
- A FinalPing account with an active license

## Quick start

1. Install the dependency:
   pip3 install requests

2. Open finalping_ground.py and fill in the config at the top:
   - FINALPING_EMAIL and FINALPING_PASSWORD
   - Your coordinates (MY_LAT, MY_LON, MY_ELEVATION_FT)
   - Your aircraft tail numbers and ICAO24 hex codes
   - Your dump1090 address (usually http://localhost:8080)

3. Run it:
   python3 finalping_ground.py

That's it. You'll see alerts appear in your FinalPing logs and
notifications will fire through your existing Discord/Slack/email/SMS channels.

## Finding your ICAO24 hex code

Go to https://globe.adsbexchange.com and search for your aircraft's
tail number. The hex code is shown in the aircraft details panel.
It looks like: a1b2c3

## Auto-start on boot (Raspberry Pi)

Copy the service file and enable it:

  sudo cp finalping-ground.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable finalping-ground
  sudo systemctl start finalping-ground

Check the logs:
  sudo journalctl -u finalping-ground -f

## What it detects

- Takeoff — aircraft transitions from ground to airborne above 60 knots
- Landing — aircraft transitions from airborne to on-ground
- 10nm alert — aircraft crosses 10 nautical miles inbound
- 5nm alert — aircraft crosses 5 nautical miles inbound
- 2nm alert — aircraft crosses 2 nautical miles inbound

All alerts fire through your existing FinalPing notification channels
(Discord, Slack, Teams, email, SMS, WhatsApp) and appear in your
alert history on finalpingapp.com/dashboard.

## Receiver not on the same device?

If your dump1090 is running on a different computer or Pi on your
network, change DUMP1090_URL to its IP address:

  DUMP1090_URL = "http://192.168.1.50:8080/data/aircraft.json"

## Supported receiver software

- dump1090 (any version)
- dump1090-fa (FlightAware)
- dump1090-mutability
- readsb
- PiAware

All of these expose the same JSON format at port 8080.
