"""Open-Meteo weather fetched on the PC, with bounded cache and provenance."""
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import time

import aiohttp
from local_tools import BEIJING, UtilityResult

GEOCODING = 'https://geocoding-api.open-meteo.com/v1/search'
FORECAST = 'https://api.open-meteo.com/v1/forecast'


class WeatherUnavailable(ValueError):
    pass


def conditions(code):
    if code == 0:
        return '晴'
    if code in (1, 2):
        return '多云'
    if code == 3:
        return '阴'
    if code in (45, 48):
        return '有雾'
    if code in (51, 53, 55, 56, 57):
        return '毛毛雨'
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return '有雨'
    if code in (71, 73, 75, 77, 85, 86):
        return '有雪'
    if code in (95, 96, 99):
        return '雷雨'
    return '天气状况未知'


class Weather:
    def __init__(self, path, default_city='合肥', clock=time.time):
        self.path = Path(path)
        self.city = default_city
        self.clock = clock
        self.locations = {}
        self.cache = {}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(saved, dict) or not isinstance(saved.get('city'), str) or not 2 <= len(saved['city']) <= 20:
                    raise ValueError('Invalid city')
                self.city = saved['city']
            except (ValueError, TypeError, KeyError) as exc:
                raise RuntimeError('天气城市配置无法读取，原文件已保留。') from exc

    async def fetch(self, url, params):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        raise WeatherUnavailable('天气接口暂时不可用，请稍后再试。')
                    result = await response.json()
                    if not isinstance(result, dict) or result.get('error'):
                        raise WeatherUnavailable('天气接口返回异常，请稍后再试。')
                    return result
        except WeatherUnavailable:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise WeatherUnavailable('暂时连不上天气服务，请检查电脑网络。') from None

    async def location(self, city):
        if city in self.locations:
            return self.locations[city]
        data = await self.fetch(GEOCODING, {'name': city, 'count': 5, 'language': 'zh', 'format': 'json'})
        candidates = data.get('results', [])
        if not candidates:
            raise WeatherUnavailable('没有找到这个城市，请换一个明确的城市名称。')
        if (not isinstance(candidates, list) or any(not isinstance(row, dict)
                or not isinstance(row.get('name'), str)
                or type(row.get('population', 0)) not in (int, float)
                or not math.isfinite(row.get('population', 0)) for row in candidates)):
            raise WeatherUnavailable('城市信息返回异常，请稍后再试。')
        # Chinese queries prefer a Chinese result; expose the actual place name
        # in output instead of silently treating a city as the user's location.
        candidates = sorted(candidates, key=lambda row: (row.get('country_code') != 'CN', -row.get('population', 0)))
        location = candidates[0]
        if any(type(location.get(k)) not in (int, float) or not math.isfinite(location[k]) for k in ('latitude', 'longitude')):
            raise WeatherUnavailable('城市坐标返回异常，请稍后再试。')
        if not -90 <= location['latitude'] <= 90 or not -180 <= location['longitude'] <= 180:
            raise WeatherUnavailable('城市坐标返回异常，请稍后再试。')
        self.locations[city] = location
        return location

    async def apply(self, command):
        if command.operation == 'location':
            return UtilityResult('默认天气城市是' + self.city + '。', self.city + '天气')
        city = command.argument or self.city
        location = await self.location(city)
        if command.operation == 'city':
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'city': city}, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.path)
            self.city = city
            return UtilityResult('默认天气城市已设为' + city + '。', city + '天气')
        now = self.clock()
        key = (location['latitude'], location['longitude'])
        cached = self.cache.get(key)
        stale = False
        if cached and now - cached[1] <= 600:
            data, fetched = cached
        else:
            try:
                data = await self.fetch(FORECAST, {
                    'latitude': location['latitude'], 'longitude': location['longitude'],
                    'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max',
                    'forecast_days': 3, 'timezone': 'Asia/Shanghai'})
                # Validate before caching; malformed results cannot replace good data.
                self.validate(data)
                fetched = now
                self.cache[key] = (data, fetched)
            except WeatherUnavailable:
                if not cached or now - cached[1] > 3600:
                    raise
                data, fetched = cached
                stale = True
        target = datetime.fromtimestamp(now, BEIJING).date()
        target += timedelta(days=int(command.operation))
        dates = data['daily']['time']
        if target.isoformat() not in dates:
            raise WeatherUnavailable('没有这个日期的有效天气预报，请稍后再查。')
        index = dates.index(target.isoformat())
        daily = data['daily']
        high = daily['temperature_2m_max'][index]
        low = daily['temperature_2m_min'][index]
        code = daily['weather_code'][index]
        rain = daily['precipitation_probability_max'][index]
        day = {'0': '今天', '1': '明天', '2': '后天'}[command.operation]
        updated = datetime.fromtimestamp(fetched, BEIJING).strftime('%H:%M')
        name = location.get('name') or city
        summary = conditions(code)
        text = f'{name}{day}{summary}，最低{low:g}度，最高{high:g}度，最高降水概率百分之{rain:g}。'
        if command.label == 'rain':
            text += '降水概率是预报，并不能保证一定下雨或不下雨。'
        text += f'这是{updated}获取的Open Meteo预报。'
        if stale:
            text = '网络暂不可用，以下是缓存预报。' + text
        # A short caption fits Mini3's existing 42-byte UTF-8 transport budget.
        caption = f'{city[:3]}{summary[:2]}{low:g}~{high:g} {updated}'
        return UtilityResult(text, caption)

    @staticmethod
    def validate(data):
        try:
            daily = data['daily']
            dates = daily['time']
            if not isinstance(dates, list) or not 1 <= len(dates) <= 16:
                raise ValueError()
            for date in dates:
                datetime.fromisoformat(date)
            for name in ('weather_code', 'temperature_2m_max', 'temperature_2m_min', 'precipitation_probability_max'):
                values = daily[name]
                if not isinstance(values, list) or len(values) != len(dates) or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                    raise ValueError()
            if any(not 0 <= v <= 100 for v in daily['precipitation_probability_max']):
                raise ValueError()
            if any(not -100 <= v <= 80 for v in daily['temperature_2m_max'] + daily['temperature_2m_min']):
                raise ValueError()
            if any(low > high for low, high in zip(daily['temperature_2m_min'], daily['temperature_2m_max'])):
                raise ValueError()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise WeatherUnavailable('天气数据缺失或异常，请稍后再试。') from None
