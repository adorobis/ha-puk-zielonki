"""Config flow: IMAP mailbox that receives the PUK Zielonki invoice notifications."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .const import (
    CONF_FOLDER,
    CONF_SENDER,
    DEFAULT_FOLDER,
    DEFAULT_PORT,
    DEFAULT_SENDER,
    DEFAULT_SERVER,
    DOMAIN,
    METER_GARDEN,
    METER_MAIN,
)
from .coordinator import reference_key
from .mail import MailAuthError, MailClient, MailError

_LOGGER = logging.getLogger(__name__)

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _test(data: dict[str, Any]) -> int:
    """Blocking: log in, select the folder and count matching messages."""
    with MailClient(
        data[CONF_HOST], data[CONF_PORT], data[CONF_USERNAME], data[CONF_PASSWORD]
    ) as client:
        client.select(data.get(CONF_FOLDER, ""))
        return len(client.search_from(data[CONF_SENDER]))


class PukZielonkiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PukOptionsFlow:
        return PukOptionsFlow()

    def _imap_entry(
        self, host: str | None = None, user: str | None = None
    ) -> dict[str, Any] | None:
        """Credentials of an existing core IMAP integration entry, if any."""
        for entry in self.hass.config_entries.async_entries("imap"):
            d = entry.data
            if (host is None or d.get("server") == host) and (
                user is None or d.get("username") == user
            ):
                return dict(d)
        return None

    async def _validate(self, data: dict[str, Any]) -> tuple[dict[str, str], int]:
        try:
            count = await self.hass.async_add_executor_job(_test, data)
        except MailAuthError:
            return {"base": "invalid_auth"}, 0
        except MailError as err:
            _LOGGER.warning("IMAP check failed: %s", err)
            return {"base": "cannot_connect"}, 0
        if count == 0:
            return {"base": "no_messages"}, 0
        return {}, count

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        imap = self._imap_entry() or {}
        if user_input is not None:
            data = {
                CONF_HOST: user_input[CONF_HOST].strip(),
                CONF_PORT: int(user_input[CONF_PORT]),
                CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                CONF_PASSWORD: user_input.get(CONF_PASSWORD) or "",
                CONF_FOLDER: (user_input.get(CONF_FOLDER) or "").strip(),
                CONF_SENDER: user_input[CONF_SENDER].strip(),
            }
            if not data[CONF_PASSWORD]:
                # Reuse the password of a matching core IMAP entry.
                match = self._imap_entry(data[CONF_HOST], data[CONF_USERNAME])
                data[CONF_PASSWORD] = (match or {}).get("password", "")
            if not data[CONF_PASSWORD]:
                errors["base"] = "password_required"
            else:
                errors, _ = await self._validate(data)
            if not errors:
                return self.async_create_entry(
                    title=f"PUK Zielonki ({data[CONF_USERNAME]})", data=data
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=imap.get("server", DEFAULT_SERVER)): str,
                vol.Required(CONF_PORT, default=imap.get("port", DEFAULT_PORT)): int,
                vol.Required(CONF_USERNAME, default=imap.get("username", "")): str,
                vol.Optional(CONF_PASSWORD): _PASSWORD,
                vol.Optional(CONF_FOLDER, default=DEFAULT_FOLDER): str,
                vol.Required(CONF_SENDER, default=DEFAULT_SENDER): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            errors, _ = await self._validate(data)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )


class PukOptionsFlow(OptionsFlowWithReload):
    """Pick a reference sensor (e.g. a pulse meter total) for each meter."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        coordinator = getattr(self.config_entry, "runtime_data", None)
        properties = coordinator.data.properties if coordinator and coordinator.data else {}
        meters = [
            (account, meter)
            for account, prop in sorted(properties.items())
            for meter in (METER_MAIN, METER_GARDEN)
            if meter in prop.meters
        ]
        if not meters:
            return self.async_abort(reason="no_meters")
        keys = {reference_key(a, m): (a, m) for a, m in meters}

        if user_input is not None:
            # Keep options of properties not shown now; drop cleared fields.
            options = {k: v for k, v in self.config_entry.options.items() if k not in keys}
            options.update({k: v for k, v in user_input.items() if k in keys and v})
            return self.async_create_entry(data=options)

        selector = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="water"))
        schema = vol.Schema(
            {
                vol.Optional(
                    key,
                    description={"suggested_value": self.config_entry.options.get(key)},
                ): selector
                for key in keys
            }
        )
        labels = {"main": "licznik główny / main meter", "garden": "licznik ogrodowy / garden"}
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "meters": "\n".join(f"- `{k}`: {a} {labels[m]}" for k, (a, m) in keys.items())
            },
        )
