Hi! You are here because I wanted you to help me with debugging.
Don't be afraid, it's an easy task for 5 minutes. 😊 Please follow the guide:

1. Find your `configuration.yaml` file in Home Assistant.
2. Find [a `logger` section](https://www.home-assistant.io/integrations/logger/) inside that file and paste the following:
    ```yaml
    logger:
      logs:
        hikconnect: debug
        custom_components.hikconnect: debug
    ```
    If the `logger` section doesn't exist yet, please create it. If it exists, manually merge the contents.
3. Restart your Home Assistant.
4. Go to _Configuration > Logs_ and press "Load Full Home Assistant Log" button.
5. Copy the full log (optionally: filter only lines, which contains "hikconnect" string).
6. Send the log to me **privately**: ja@tbedrich.cz.

**Do not post the logs publicly.** They contain sensitive information (`session_id`, `refresh_session_id` + your intercom device ID + serial number).
