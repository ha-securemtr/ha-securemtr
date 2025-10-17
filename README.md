[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
![Home Assistant >=2025.1.0](https://img.shields.io/badge/Home%20Assistant-%3E%3D2025.1.0-41BDF5.svg)

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/ha-securemtr/ha-securemtr)](https://github.com/ha-securemtr/ha-securemtr/releases)

[![Tests](https://github.com/ha-securemtr/ha-securemtr/actions/workflows/tests.yml/badge.svg)](https://github.com/ha-securemtr/ha-securemtr/actions/workflows/tests.yml)
![Coverage](docs/badges/coverage.svg)
![Python >=3.13.2](https://img.shields.io/badge/Python-%3E%3D3.13.2-blue.svg)
[![Package manager: uv](https://img.shields.io/badge/Package%20manager-uv-5F45BA?logo=astral&logoColor=white)](https://docs.astral.sh/uv/)
[![Code style: Ruff](https://img.shields.io/badge/Code%20style-Ruff-4B32C3.svg)](https://docs.astral.sh/ruff/)

![🌍 26 Languages](https://img.shields.io/badge/%F0%9F%8C%8D-26_languages-00bcd4?style=flat-square)

#  Home Assistant integration for E7+ Secure Meters Smart Water Heater Controller

Control your **Secure Meters E7+ Smart Water Heater Controller** from **Home Assistant** — in the HA app, automations, scenes, and with voice assistants.

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ha-securemtr&repository=ha-securemtr&category=integration)
[![Open your Home Assistant instance and start setting up the integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=securemtr)

> Install the integration (via HACS or manual copy) before you use the “Add integration” button.

## Who is this for?

For anyone using the **E7+ Wifi-enabled Smart Water Heater Controller** from Secure Meters. If you already manage your water heater with the Secure Controls mobile app and want the same control inside Home Assistant, this add-on is for you.

---

## What you can do in Home Assistant

- Turn the **primary** and **boost** immersion heaters on or off.
- Run the water heater during **off-peak rate hours**.
- Trigger a **boost** cycle for quick hot water when you need it.
- View daily energy use and total heating time for each heater.
- Create and adjust **weekly schedules** for both normal heating and boost periods.
- Use Home Assistant **automations**, **scenes**, and **voice assistants** to manage hot water.
- Add energy sensors to the **Energy Dashboard** to track costs over time.

---

## What you’ll need

- A working E7+ controller connected to your Wifi with the Secure Controls app.
- Your Secure Controls account **email** and **password**.
- Home Assistant (Core, OS, or Container) with internet access.

---

## Install (step-by-step)

### Option A — HACS (recommended)

1. Open **HACS → Integrations** in Home Assistant.
2. Click **⋮** → **Custom repositories** → **Add**.
3. Paste `https://github.com/ha-securemtr/ha-securemtr` and pick **Integration**.
   Or click the badge above to fill this in automatically.
4. Search for **SecureMTR** in HACS and click **Download** / **Install**.
5. **Restart Home Assistant** when prompted.

### Option B — Manual install

1. Download the latest release from GitHub.
2. Copy `custom_components/securemtr` into `<config>/custom_components/securemtr` on your Home Assistant system.
3. **Restart Home Assistant**.

---

## Set up the integration

1. Go to **Settings → Devices & Services → Add Integration** and search for **SecureMTR**, or use the badge above.
2. Sign in with the same **email** and **password** you use in the Secure Controls app.
3. Finish the setup. The water heater and boost controls appear as devices and entities you can add to dashboards or automations.

---

## Tips

- **Boost on demand:** Create a one-tap button in a dashboard to trigger a boost cycle before showers or laundry.
- **Schedule around tariffs:** Align the weekly heating timetable with your off-peak electricity rates for lower bills.
- **Energy tracking:** Add the energy sensors to Home Assistant’s Energy Dashboard to see usage trends and cost estimates.

---

## Timed boost controls

You now have four buttons in Home Assistant for quick boost actions:

- **Boost 30 minutes**
- **Boost 60 minutes**
- **Boost 120 minutes**
- **Cancel Boost** (only shown while a boost is running)

Tap a button on your dashboard, call it from the Services panel, or trigger it in an automation whenever you want hot water without opening the Secure Controls app.

## Timed boost sensors

Two new sensors help you see what the heater is doing:

- **Boost Active** shows if a boost is running right now.
- **Boost Ends** counts down to when the current boost will switch off.

Add them to a dashboard card or use them in automations—for example, to skip starting another boost while one is already underway.
---

## Troubleshooting

- **Can’t sign in?** Double-check your details in the Secure Controls app first, then re-enter them here.
- **No devices appear?** Make sure the controller is online in the Secure Controls app and that your Home Assistant has internet access.
- **Need help?** Open a GitHub issue with your controller model and a short description of the problem. Do not share passwords or personal information.

---

## Privacy & security

- Your login stays in Home Assistant; it isn’t shared with anyone else.
- This project is a community effort and is **not affiliated** with Secure Meters or Home Assistant.

---

## Development quick start

Prepare the environment:

```bash
uv venv -p 3.13
uv pip install --all-extras -r pyproject.toml -p 3.13
```

Run tests with coverage:

```bash
uv run pytest --cov=custom_components/securemtr --cov-report=term-missing
```

See [`docs/developer-notes.md`](docs/developer-notes.md) for more information.

---

## Search keywords

*SecureMTR Home Assistant, Secure Meters E7+ Home Assistant, Smart water heater boost Home Assistant, Secure water heater schedule, Secure Controls Home Assistant*

*This repository is not affiliated with Secure Meters (UK), the Secure Controls system, Home Assistant, or any other referenced entities. Use at your own risk.*
