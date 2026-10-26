"""Input services: one per reading the bot takes before picking tracks."""

from .base import InputService
from .citibike import CitiBikeService
from .day_of_week import DayOfWeekService
from .dj_roll import DjRollService
from .g_train import GTrainService
from .nyc_311 import Nyc311Service
from .sunset import SunsetService
from .tide import TideService
from .weather import WeatherService

# Display order on the site.
SERVICES: list[InputService] = [
    DjRollService(),
    DayOfWeekService(),
    WeatherService(),
    GTrainService(),
    SunsetService(),
    TideService(),
    CitiBikeService(),
    Nyc311Service(),
]
