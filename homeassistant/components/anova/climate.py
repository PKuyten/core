"""Support for Anova Bluetooth LE sous vides."""
import logging
import threading
from uuid import UUID
from typing import Any, Dict, List, Optional
from pexpect.exceptions import ExceptionPexpect
from pygatt.backends.gatttool.gatttool import NotConnectedError, NotificationTimeout

import voluptuous as vol

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
from homeassistant.components.climate.const import (
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    SUPPORT_TARGET_TEMPERATURE,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_MODE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_STATE,
    ATTR_TEMPERATURE,
    CONF_MAC,
    CONF_NAME,
    CONF_TEMPERATURE_UNIT,
    EVENT_HOMEASSISTANT_STOP,
    TEMP_CELSIUS,
    TEMP_FAHRENHEIT,
    PRECISION_WHOLE,
    PRECISION_HALVES,
    PRECISION_TENTHS,
)
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers import config_validation as cv
from pyanova.pyanova import PyAnova, CTL_START, CTL_STOP

from homeassistant.util.temperature import convert as convert_temperature

_LOGGER = logging.getLogger(__name__)

DEFAULT_MIN_TEMP = 0
DEFAULT_MAX_TEMP = 99

DEFAULT_CONNECT_TIMEOUT_SEC = 30
DEFAULT_CMD_TIMEOUT_SEC = 10
POLLING_CYCLE_SEC = 30

DEVICE_NOTIFICATION_CHAR_HANDLE_INDICATION = 0x25
DEVICE_NOTIFICATION_CHAR_HANDLE_NOTIFICATION = 0x12

DEFAULT_NAME = "Anova"

ATTR_NAME = CONF_NAME
ATTR_MAC = CONF_MAC
ATTR_TEMPERATURE_UNIT = CONF_TEMPERATURE_UNIT
ANOVA_DEVICE_NAME = ATTR_NAME
ANOVA_DEVICE_ADDRESS = "address"

ATTR_TARGET_TEMPERATURE = "target_temperature"
ATTR_AVAILABLE = "available"
ANOVA_STATE_RUNNING = "running"
ANOVA_STATE_STOPPED = "stopped"
ANOVA_TEMP_CELCIUS = "c"
ANOVA_TEMP_FAHRENHEIT = "f"

TEMP_MAP = {ANOVA_TEMP_CELCIUS: TEMP_CELSIUS, ANOVA_TEMP_FAHRENHEIT: TEMP_FAHRENHEIT}
CTL_MAP = {CTL_START: HVAC_MODE_HEAT, CTL_STOP: HVAC_MODE_OFF}

MON_GET_TEMPERATURE_UNIT = 1
MON_GET_STATUS = 2
MON_GET_CURRENT_TEMPERATURE = 3
MON_GET_TARGET_TEMPERATURE = 4
MON_IDLE = 5

CTL = "set"
ATTR_CTL_HVAC_MODE = "{CTL}_{ATTR_HVAC_MODE}"
ATTR_CTL_TARGET_TEMPERATURE = "{CTL}_{ATTR_TARGET_TEMPERATURE}"
ATTR_CTL_TEMPERATURE_UNIT = "{CTL}_{ATTR_TEMPERATURE_UNIT}"

HVAC_MODES = [HVAC_MODE_OFF, HVAC_MODE_HEAT]
SUPPORTED_FEATURES = SUPPORT_TARGET_TEMPERATURE

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(ATTR_MAC): cv.string,
        vol.Optional(ATTR_NAME, default=DEFAULT_NAME): cv.string,
    }
)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Anova device."""

    _LOGGER.debug("Setting up...")

    name = config.get(ATTR_NAME)

    data = {
        ATTR_NAME: name,
        ATTR_MAC: config.get(ATTR_MAC),
        ATTR_AVAILABLE: False,
        ATTR_CURRENT_TEMPERATURE: None,
        ATTR_CTL_HVAC_MODE: None,
        ATTR_HVAC_MODE: None,
        ATTR_CTL_TARGET_TEMPERATURE: None,
        ATTR_TARGET_TEMPERATURE: None,
        ATTR_CTL_TEMPERATURE_UNIT: hass.config.units.temperature_unit,
        ATTR_TEMPERATURE_UNIT: hass.config.units.temperature_unit,
    }

    monitor = AnovaMonitor(data)
    climate = AnovaClimate(monitor)
    monitor.set_data_handler(climate.schedule_update_ha_state)

    add_entities([climate])

    def monitor_stop(_service_or_event):
        """Stop the monitor thread."""
        _LOGGER.info("Stopping monitor for %s", name)
        monitor.terminate()

    monitor.start()
    hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, monitor_stop)


class AnovaClimate(ClimateEntity):
    """Representation of a Anova device."""

    def __init__(self, monitor):
        """Build AnovaClimate.

        monitor: Anova monitor
        """

        self._monitor = monitor

    @property
    def should_poll(self) -> bool:
        """Return True if entity has to be polled for state.

        False if entity pushes its state to HA.
        """
        return False

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._monitor.data[ATTR_MAC]

    @property
    def supported_features(self) -> int:
        """Return the list of supported features."""
        return SUPPORTED_FEATURES

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement which this thermostat uses."""
        return self._monitor.data[ATTR_TEMPERATURE_UNIT]

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._monitor.data[ATTR_AVAILABLE]

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._monitor.data[ATTR_TARGET_TEMPERATURE]

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        if self.temperature_unit == TEMP_CELSIUS:
            return PRECISION_HALVES
        return PRECISION_WHOLE

    @property
    def hvac_mode(self):
        """Return current operation ie. heat, idle."""
        return self._monitor.data[ATTR_HVAC_MODE]

    @property
    def current_temperature(self) -> Optional[float]:
        """Return the current temperature."""
        return self._monitor.data[ATTR_CURRENT_TEMPERATURE]

    @property
    def hvac_modes(self) -> List[str]:
        """List of available operation modes."""
        return HVAC_MODES

    @property
    def name(self):
        """Return the name of the entity."""
        return self._monitor.data[ATTR_NAME]

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return convert_temperature(
            DEFAULT_MIN_TEMP, TEMP_CELSIUS, self.temperature_unit
        )

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        return convert_temperature(
            DEFAULT_MAX_TEMP, TEMP_CELSIUS, self.temperature_unit
        )

    def set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        if temperature < self.min_temp:
            temperature = self.min_temp
        elif temperature > self.max_temp:
            temperature = self.max_temp

        self._monitor.request_set_temperature(temperature)

    def set_hvac_mode(self, hvac_mode: str):
        """Set new target operation mode."""
        self._monitor.request_set_hvac_mode(hvac_mode)


class AnovaMonitor(threading.Thread):
    """Connection handling."""

    def __init__(self, data):
        """Construct interface object."""
        threading.Thread.__init__(self)
        self.daemon = False
        self.data = data
        self.keep_going = True
        self.command_event = threading.Event()
        self.data_handler = None
        self.device = None

        self.connect_timeout = DEFAULT_CONNECT_TIMEOUT_SEC
        self.command_timout = DEFAULT_CMD_TIMEOUT_SEC
        self.polling_cycle = POLLING_CYCLE_SEC

    def set_data_handler(self, handler):
        self.data_handler = handler

    def process_response(self, value, key):
        if value == None:
            _LOGGER.warn("Received %s for %s: %s", self.name, key, str(value))
        elif value != self.data[key]:
            self.data[key] = value

            if self.data[ATTR_AVAILABLE]:
                self.data_handler()

    def handle_map(self, rsp, values):
        if rsp in values:
            return values[rsp]
        return None

    def handle_float(self, rsp):
        try:
            value = float(rsp)
        except:
            value = None

        return value

    def get_temperature_unit(self):
        rsp = self.device.get_unit(
            handle=DEVICE_NOTIFICATION_CHAR_HANDLE_NOTIFICATION,
            timeout=self.command_timout,
        )
        value = self.handle_map(
            rsp,
            {ANOVA_TEMP_CELCIUS: TEMP_CELSIUS, ANOVA_TEMP_FAHRENHEIT: TEMP_FAHRENHEIT},
        )
        self.process_response(value, CONF_TEMPERATURE_UNIT)

    def set_temperature_unit(self, unit: str):
        rsp = self.device.set_unit(
            handle=DEVICE_NOTIFICATION_CHAR_HANDLE_NOTIFICATION,
            unit=unit,
            timeout=self.command_timout,
        )
        value = self.handle_map(
            rsp,
            TEMP_MAP,
        )
        self.process_response(value, CONF_TEMPERATURE_UNIT)

    def get_status(self):
        rsp = self.device.get_status(
            handle=DEVICE_NOTIFICATION_CHAR_HANDLE_NOTIFICATION,
            timeout=self.command_timout,
        )
        value = self.handle_map(
            rsp,
            {ANOVA_STATE_RUNNING: HVAC_MODE_HEAT, ANOVA_STATE_STOPPED: HVAC_MODE_OFF},
        )
        self.process_response(value, ATTR_HVAC_MODE)

    def get_current_temperature(self):
        rsp = self.device.get_current_temperature(
            handle=DEVICE_NOTIFICATION_CHAR_HANDLE_NOTIFICATION,
            timeout=self.command_timout,
        )
        value = self.handle_float(rsp)
        self.process_response(value, ATTR_CURRENT_TEMPERATURE)

    def get_target_temperature(self):
        rsp = self.device.get_target_temperature(
            handle=DEVICE_NOTIFICATION_CHAR_HANDLE_NOTIFICATION,
            timeout=self.command_timout,
        )
        value = self.handle_float(rsp)
        self.process_response(value, ATTR_TARGET_TEMPERATURE)

    def send_start(self, start: bool):
        if start:
            rsp = self.device.start_anova(
                handle=DEVICE_NOTIFICATION_CHAR_HANDLE_NOTIFICATION,
                timeout=self.command_timout,
            )
        else:
            rsp = self.device.stop_anova(
                handle=DEVICE_NOTIFICATION_CHAR_HANDLE_NOTIFICATION,
                timeout=self.command_timout,
            )

        value = self.handle_map(rsp, CTL_MAP)
        self.process_response(value, ATTR_HVAC_MODE)

    def set_target_temperature(self, temperature):
        rsp = self.device.set_temperature(
            target_temp=temperature,
            handle=DEVICE_NOTIFICATION_CHAR_HANDLE_NOTIFICATION,
            timeout=self.command_timout,
        )
        value = self.handle_map(rsp, {str(temperature): temperature})
        self.process_response(value, ATTR_TARGET_TEMPERATURE)

    def request_set_hvac_mode(self, hvac_mode: str):
        self.data[ATTR_CTL_HVAC_MODE] = hvac_mode
        self.data[ATTR_HVAC_MODE] = hvac_mode
        self.command_event.set()
        self.data_handler()

    def request_set_temperature(self, temperature: float):
        self.data[ATTR_CTL_TARGET_TEMPERATURE] = temperature
        self.data[ATTR_TARGET_TEMPERATURE] = temperature
        self.command_event.set()
        self.data_handler()

    def request_set_temperature_unit(self, unit: str):
        if unit != self.data[CONF_TEMPERATURE_UNIT]:
            self.data[ATTR_CTL_TEMPERATURE_UNIT] = unit
            self.data[ATTR_TEMPERATURE_UNIT] = unit
            self.command_event.set()
            self.data_handler()

    def run(self):
        """Thread that keeps connection alive."""

        self.device = PyAnova(
            auto_connect=False, logger=_LOGGER, debug=False, use_handle=False
        )

        while self.keep_going:
            if self.data[ATTR_AVAILABLE]:
                self.data[ATTR_AVAILABLE] = False
                self.data_handler()

            try:

                _LOGGER.debug(
                    "Connecting to {0} with timeout after {1} seconds".format(
                        self.name, self.connect_timeout
                    )
                )
                self.device.connect_device(
                    {
                        ANOVA_DEVICE_NAME: self.data[ATTR_NAME],
                        ANOVA_DEVICE_ADDRESS: self.data[ATTR_MAC],
                    },
                    indication=False,
                    reset_on_start=False,
                    timeout=self.connect_timeout,
                )

                _LOGGER.debug(
                    "Subscribed to %s (%s)"
                    % (self.data[ATTR_NAME], self.data[ATTR_MAC])
                )

                next_command = MON_GET_TEMPERATURE_UNIT

                while self.keep_going:
                    available = (
                        self.data[ATTR_CURRENT_TEMPERATURE] != None
                        and self.data[ATTR_TARGET_TEMPERATURE] != None
                        and self.data[ATTR_TEMPERATURE_UNIT] != None
                    )

                    if not self.data[ATTR_AVAILABLE] and available:
                        self.data[ATTR_AVAILABLE] = True
                        self.data_handler()

                    if self.data[ATTR_CTL_TEMPERATURE_UNIT] != None:
                        if self.data[ATTR_CTL_TEMPERATURE_UNIT] == TEMP_CELSIUS:
                            unit = ANOVA_TEMP_CELCIUS
                        else:
                            unit = ANOVA_TEMP_FAHRENHEIT
                        self.set_temperature_unit(unit)
                        self.data[ATTR_CTL_TEMPERATURE_UNIT] = None
                    elif self.data[ATTR_CTL_TARGET_TEMPERATURE] != None:
                        self.set_target_temperature(
                            self.data[ATTR_CTL_TARGET_TEMPERATURE]
                        )
                        self.data[ATTR_CTL_TARGET_TEMPERATURE] = None
                    elif self.data[ATTR_CTL_HVAC_MODE] != None:
                        self.send_start(
                            self.data[ATTR_CTL_HVAC_MODE] == HVAC_MODE_HEAT,
                        )
                        self.data[ATTR_CTL_HVAC_MODE] = None
                    elif next_command == MON_GET_TEMPERATURE_UNIT:
                        self.get_temperature_unit()
                        next_command = MON_GET_STATUS
                    elif next_command == MON_GET_STATUS:
                        self.get_status()
                        next_command = MON_GET_CURRENT_TEMPERATURE
                    elif next_command == MON_GET_CURRENT_TEMPERATURE:
                        self.get_current_temperature()
                        next_command = MON_GET_TARGET_TEMPERATURE
                    elif next_command == MON_GET_TARGET_TEMPERATURE:
                        self.get_target_temperature()
                        next_command = MON_IDLE
                    else:
                        next_command = MON_GET_STATUS
                        self.command_event.clear()
                        self.command_event.wait(self.polling_cycle)
                break
            except ExceptionPexpect as ex:
                self.keep_going = False
                self.device = None
            except NotConnectedError as ex:
                _LOGGER.error("Unable to connect: %s", str(ex))
            except NotificationTimeout as ex:
                _LOGGER.error("Unable to subscribe: %s", str(ex))
            except RuntimeError as ex:
                _LOGGER.error("Unable to send command: %s", str(ex))
            except Exception as ex:
                _LOGGER.warn(ex, exc_info=True)
            finally:
                if self.data[ATTR_AVAILABLE]:
                    self.data[ATTR_AVAILABLE] = False
                    self.data_handler()

                self.current_cmd = None

                try:
                    if self.device:
                        self.device.disconnect()
                except Exception as ex:
                    _LOGGER.debug(ex, exc_info=True)

    def terminate(self):
        """Signal runner to stop and join thread."""
        self.keep_going = False

        self.command_event.set()
        self.join()