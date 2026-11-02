"""Three local utility modules shared by voice and --text input."""
from pathlib import Path

from local_tools import UtilityResult, parse_utility, tool_answer
from reminders import Reminders
from weather import Weather


class Utilities:
    def __init__(self, runtime):
        runtime = Path(runtime)
        self.reminders = Reminders(runtime / 'reminders.json')
        self.weather = Weather(runtime / 'weather-settings.json')

    async def apply(self, command):
        try:
            if command.kind == 'reminder':
                return self.reminders.apply(command)
            if command.kind == 'weather':
                return await self.weather.apply(command)
            return tool_answer(command)
        except ValueError as exc:
            return UtilityResult(str(exc), '请调整指令后重试')
        except OSError:
            return UtilityResult('本地配置保存失败，请检查电脑存储。', '本地保存失败')
