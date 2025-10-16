#  Home Assistant integration for the E7+ by Secure Meters smart water heater controller

*_Disclaimer: This repository is not in any way affiliated with Secure Meters (UK), the Secure Controls mobile app, Home Assistant or other referenced entities. This is an open community project to allow customers to use their electric water heater controllers with their own authorized credentials. Use at your own risk._*

## Configuration

- Provide the same email address and password that you use with the Secure Controls mobile app when setting up the securemtr integration.
- The Secure Controls mobile app limits passwords to a maximum of **12 characters**. The securemtr config flow enforces this limit and rejects longer passwords.
- Passwords are never stored in plaintext. The securemtr integration saves only the lowercase hexadecimal MD5 digest that is sent to the Secure Controls backend.
