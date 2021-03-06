"""SAJ solar inverter interface."""
import asyncio
from datetime import date
import logging

import pysaj
import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_HOST,
    DEVICE_CLASS_POWER,
    DEVICE_CLASS_TEMPERATURE,
    ENERGY_KILO_WATT_HOUR,
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STOP,
    MASS_KILOGRAMS,
    POWER_WATT,
    TEMP_CELSIUS,
    TEMP_FAHRENHEIT,
)
from homeassistant.core import CALLBACK_TYPE, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later

_LOGGER = logging.getLogger(__name__)

MIN_INTERVAL = 5
MAX_INTERVAL = 300

UNIT_OF_MEASUREMENT_HOURS = "h"

SAJ_UNIT_MAPPINGS = {
    "W": POWER_WATT,
    "kWh": ENERGY_KILO_WATT_HOUR,
    "h": UNIT_OF_MEASUREMENT_HOURS,
    "kg": MASS_KILOGRAMS,
    "°C": TEMP_CELSIUS,
    "": None,
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({vol.Required(CONF_HOST): cv.string})


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up SAJ sensors."""

    remove_interval_update = None

    # Init all sensors
    sensor_def = pysaj.Sensors()

    # Use all sensors by default
    hass_sensors = []

    for sensor in sensor_def:
        hass_sensors.append(SAJsensor(sensor))

    saj = pysaj.SAJ(config[CONF_HOST])

    async_add_entities(hass_sensors)

    async def async_saj():
        """Update all the SAJ sensors."""
        tasks = []

        values = await saj.read(sensor_def)

        for sensor in hass_sensors:
            state_unknown = False
            if not values:
                # SAJ inverters are powered by DC via solar panels and thus are
                # offline after the sun has set. If a sensor resets on a daily
                # basis like "today_yield", this reset won't happen automatically.
                # Code below checks if today > day when sensor was last updated
                # and if so: set state to None.
                # Sensors with live values like "temperature" or "current_power"
                # will also be reset to None.
                if (sensor.per_day_basis and date.today() > sensor.date_updated) or (
                    not sensor.per_day_basis and not sensor.per_total_basis
                ):
                    state_unknown = True
            task = sensor.async_update_values(unknown_state=state_unknown)
            if task:
                tasks.append(task)
        if tasks:
            await asyncio.wait(tasks)
        return values

    def start_update_interval(event):
        """Start the update interval scheduling."""
        nonlocal remove_interval_update
        remove_interval_update = async_track_time_interval_backoff(hass, async_saj)

    def stop_update_interval(event):
        """Properly cancel the scheduled update."""
        remove_interval_update()  # pylint: disable=not-callable

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, start_update_interval)
    hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, stop_update_interval)


@callback
def async_track_time_interval_backoff(hass, action) -> CALLBACK_TYPE:
    """Add a listener that fires repetitively and increases the interval when failed."""
    remove = None
    interval = MIN_INTERVAL

    async def interval_listener(now=None):
        """Handle elapsed interval with backoff."""
        nonlocal interval, remove
        try:
            if await action():
                interval = MIN_INTERVAL
            else:
                interval = min(interval * 2, MAX_INTERVAL)
        finally:
            remove = async_call_later(hass, interval, interval_listener)

    hass.async_create_task(interval_listener())

    def remove_listener():
        """Remove interval listener."""
        if remove:
            remove()  # pylint: disable=not-callable

    return remove_listener


class SAJsensor(Entity):
    """Representation of a SAJ sensor."""

    def __init__(self, pysaj_sensor):
        """Initialize the sensor."""
        self._sensor = pysaj_sensor
        self._state = self._sensor.value

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"saj_{self._sensor.name}"

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return SAJ_UNIT_MAPPINGS[self._sensor.unit]

    @property
    def device_class(self):
        """Return the device class the sensor belongs to."""
        if self.unit_of_measurement == POWER_WATT:
            return DEVICE_CLASS_POWER
        if (
            self.unit_of_measurement == TEMP_CELSIUS
            or self._sensor.unit == TEMP_FAHRENHEIT
        ):
            return DEVICE_CLASS_TEMPERATURE

    @property
    def should_poll(self) -> bool:
        """SAJ sensors are updated & don't poll."""
        return False

    @property
    def per_day_basis(self) -> bool:
        """Return if the sensors value is on daily basis or not."""
        return self._sensor.per_day_basis

    @property
    def per_total_basis(self) -> bool:
        """Return if the sensors value is cummulative or not."""
        return self._sensor.per_total_basis

    @property
    def date_updated(self) -> date:
        """Return the date when the sensor was last updated."""
        return self._sensor.date

    def async_update_values(self, unknown_state=False):
        """Update this sensor."""
        update = False

        if self._sensor.value != self._state:
            update = True
            self._state = self._sensor.value

        if unknown_state and self._state is not None:
            update = True
            self._state = None

        return self.async_update_ha_state() if update else None

    @property
    def unique_id(self):
        """Return a unique identifier for this sensor."""
        return f"{self._sensor.name}"
