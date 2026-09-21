"""情绪管理系统。

追踪和管理舰队的情绪值（心情值）。碧蓝航线中，舰船在战斗中会消耗情绪，
情绪过低会导致经验加成失效、出现负面表情等。情绪通过以下方式恢复：
- 港区休息（不在后宅）：每 6 分钟恢复 20 点
- 后宅一楼：每 6 分钟恢复 40 点
- 后宅二楼：每 6 分钟恢复 50 点
- 誓约加成：额外 +10 点/6分钟
- 温泉加成：额外 +10 点/6分钟

情绪控制策略：
- 保持开心加成（>120）：最大化经验加成
- 防止绿脸（>40）：避免负面效果
- 防止黄脸（>30）：避免严重负面效果
- 防止红脸（>2）：最低限度保护

游戏客户端存在已知 bug：长时间运行后情绪计算不准确，需要定期重启。

共用心情（PublicEmotion）：
多个任务复用同一支舰队时，按**真实舰队**而不是按任务记账，避免同一支舰队的
心情被拆成多份、或不同舰队的心情被合并成一份。真实舰队号取自任务设置里的
`Fleet.Fleet1` / `Fleet.Fleet2`（编队界面里的第几支舰队，1~6），由
`Fleet.FleetOrder` 决定谁打道中、谁打 Boss。每支真实舰队各有独立的
Value/Record/Control/Recover，因此可以跨任务共享同一支舰队的心情，
也可以让不同任务使用不同的舰队。

注意区分两个「舰队号」：`fleet_current_index` 的 1 道中 / 2 Boss 是逻辑编号，
屏幕上的 1/2 是出击位，两者都不是真实舰队号，不参与记账。
"""

from datetime import datetime, timedelta
from time import sleep

from module.base.decorator import cached_property
from module.base.utils import random_normal_distribution_int
from module.config.config import AzurLaneConfig
from module.config.time_source import now as current_time
from module.exception import ScriptEnd, ScriptError, RequestHumanTakeover
from module.logger import logger

# 情绪控制阈值：当情绪低于此值时触发等待/延迟
DIC_LIMIT = {
    'keep_exp_bonus': 120,     # 保持经验加成（心情开心）
    'prevent_green_face': 40,  # 防止绿脸
    'prevent_yellow_face': 30, # 防止黄脸
    'prevent_red_face': 2,     # 防止红脸
}
# 情绪恢复速度：每 6 分钟恢复的点数
DIC_RECOVER = {
    'not_in_dormitory': 20,    # 港区休息
    'dormitory_floor_1': 40,   # 后宅一楼
    'dormitory_floor_2': 50,   # 后宅二楼
}
# 情绪上限
DIC_RECOVER_MAX = {
    'not_in_dormitory': 119,
    'dormitory_floor_1': 150,
    'dormitory_floor_2': 150,
}
OATH_RECOVER = 10    # 誓约额外恢复速度
ONSEN_RECOVER = 10   # 温泉额外恢复速度

# 出击时最多带两支舰队。这里列的是**真实舰队号**（编队界面里的第几支舰队，
# 即 `Fleet.Fleet1` / `Fleet.Fleet2` 选的那个 1~6），不是出击位的 1/2。
# 每支真实舰队各自独立记账，被多个任务复用时共享同一份心情。
REAL_FLEETS = (1, 2, 3, 4, 5, 6)


def real_fleets_of(order, fleet_1, fleet_2):
    """按任务的舰队职能算出这次出击用的真实舰队号。

    `Fleet.Fleet1` / `Fleet.Fleet2` 是编队准备时 `ensure_to_be()` 编入出击位的
    真实舰队号（1~6），`Fleet2` 为 0 表示只带一支。屏幕上的舰队 1/2 只是出击位，
    不是真实编号，所以心情必须按真实舰队号记账。

    待命的那支不算出击，因此不会凭空扣减一支没有出击的舰队的心情。

    Args:
        order (str): `Fleet.FleetOrder` 的取值。
        fleet_1 (int): `Fleet.Fleet1`，道中位对应的真实舰队号。
        fleet_2 (int): `Fleet.Fleet2`，Boss 位对应的真实舰队号，0 表示不编入。

    Returns:
        dict[int, int]: 真实舰队号 -> 逻辑编号（1 道中、2 Boss）。
    """
    if order == 'fleet1_all_fleet2_standby':
        # 单队全清：只编入道中位，它同时承担道中与 Boss。
        return {int(fleet_1): 1} if fleet_1 else {}
    if order == 'fleet1_standby_fleet2_all':
        return {int(fleet_2): 2} if fleet_2 else {}
    # 两队分工：两支真实舰队都出击，道中位与 Boss 位各一支。
    if fleet_1 and fleet_2 and int(fleet_1) != int(fleet_2):
        return {int(fleet_1): 1, int(fleet_2): 2}
    if fleet_1:
        return {int(fleet_1): 1}
    if fleet_2:
        return {int(fleet_2): 2}
    return {}


class FleetEmotion:
    """单个舰队的情绪追踪器。

    管理一个舰队的情绪值、恢复速度和控制阈值。
    普通模式按任务的逻辑编号记账（`Emotion.FleetN*`），
    共用心情模式按真实舰队号记账（`PublicEmotion.FleetN*`）。

    Attributes:
        config (AzurLaneConfig): 配置对象。
        fleet (int | str): 真实舰队号，或共用心情模式的 `'Public'`。
        number (int): 本追踪器对应的真实舰队号（编队界面里的第几支舰队）。
        current (int): 当前计算的情绪值。
    """

    def __init__(self, config, fleet, number=None):
        """
        Args:
            config (AzurLaneConfig):
            fleet (int | str): 真实舰队号，或 `'Public'` 表示共用心情模式。
            number (int): 共用心情模式下的真实舰队号，普通模式下等于 `fleet`。
        """
        self.config = config
        self.fleet = fleet
        self.number = int(number if number is not None else fleet)
        self.current = 0
        # 未满 1 点的恢复余数，由 update() 写入、record() 回扣。
        self._fractional_seconds = 0

    @property
    def _key_prefix(self):
        if self.fleet == 'Public':
            return f'PublicEmotion_Fleet{self.number}'
        return f'Emotion_Fleet{self.fleet}'

    def _get(self, suffix):
        """读取本真实舰队的字段。

        拆分前的旧单槽位配置（`PublicEmotion.Fleet*`）由配置重定向
        `public_emotion_to_real_fleets_redirect` 在读取阶段迁移到真实舰队 1，
        因此这里只需按舰队号读取。

        Args:
            suffix (str): 字段后缀，取值与 `Emotion.FleetN*` 相同：
                Value、Record、Control、Recover、Oath、Onsen。

        Returns:
            Any: 配置值。
        """
        return getattr(self.config, f'{self._key_prefix}{suffix}')

    @property
    def value(self):
        """
        Returns:
            int: 0 到 150。
        """
        return self._get('Value')

    @property
    def value_name(self):
        """
        Returns:
            str:
        """
        return f'{self._key_prefix}Value'

    @property
    def record(self):
        """
        Returns:
            datetime.datetime:
        """
        return self._get('Record')

    @property
    def recover(self):
        """
        Returns:
            str: not_in_dormitory、dormitory_floor_1、dormitory_floor_2。
        """
        return self._get('Recover')

    @property
    def control(self):
        """
        Returns:
            str: keep_exp_bonus、prevent_green_face、prevent_yellow_face、prevent_red_face。
        """
        return self._get('Control')

    @property
    def oath(self):
        """
        Returns:
            bool: 是否所有舰船已誓约。
        """
        return self._get('Oath')

    @property
    def onsen(self):
        """
        Returns:
            bool: 是否所有舰船在温泉中。
        """
        return self._get('Onsen')

    @property
    def ignore_warning(self):
        """是否无视该舰队的红脸出击警告。

        共用心情模式下取本真实舰队的设置（每支舰队可以各配一份）；普通模式下
        没有舰队级设置，沿用任务级心情模式里的 ignore。

        Returns:
            bool: True 表示红脸照常出击，不做保底撤退与清零。
        """
        if self.fleet == 'Public':
            return self._get('IgnoreWarning')
        return 'ignore' in self.config.Emotion_Mode

    @property
    def ignore_shipwreck(self):
        """是否无视该舰队的沉船心情惩罚。

        共用心情模式下取本真实舰队的设置；普通模式沿用任务级设置。

        Returns:
            bool: True 表示沉船额外扣减不计入。
        """
        if self.fleet == 'Public':
            return self._get('IgnoreShipwreck')
        return self.config.Emotion_IgnoreShipwreck

    @property
    def speed(self):
        """
        Returns:
            int: 每 6 分钟的恢复速度。
        """
        speed = DIC_RECOVER[self.recover]
        if self.oath:
            speed += OATH_RECOVER
        if self.onsen:
            speed += ONSEN_RECOVER
        return speed // 10

    @property
    def limit(self):
        """
        Returns:
            int: 情绪控制的最低阈值。
        """
        return DIC_LIMIT[self.control]

    @property
    def max(self):
        """
        Returns:
            int: 最大情绪值。
        """
        return DIC_RECOVER_MAX[self.recover]

    def update(self):
        """根据实际经过时间计算情绪恢复。

        使用连续时间恢复计算，保留浮点恢复量以累积分数部分。
        游戏服务端按实际经过时间精确计算恢复，每6分钟恢复speed点。
        使用 int() 截断恢复量的整数部分，未满1点的余数通过
        _fractional_seconds 保留，由 record() 回扣到 Record 时间戳中，
        确保余数可跨次累积。int() 截断会导致计算值略低于实际值，
        符合情绪控制的安全方向（宁可低估也不高估）。
        """
        time_diff = current_time().timestamp() - self.record.timestamp()
        time_diff = max(time_diff, 0)
        # speed 为每360秒的恢复量，换算为每秒恢复 speed/360 点
        recovery = self.speed * time_diff / 360
        self.current = min(max(self.value, 0) + int(recovery), self.max)
        # 保留未满1点的恢复余数对应的秒数，用于 record() 回扣
        self._fractional_seconds = recovery - int(recovery)

    def get_recovered(self, expected_reduce=0):
        """计算情绪恢复到控制阈值的时间。

        Args:
            expected_reduce (int): 预期的情绪减少量。

        Returns:
            datetime.datetime: 情绪 >= 控制阈值的时间。如果已经恢复，则返回过去的时间。
        """
        if self.control == 'keep_exp_bonus' and self.recover == 'not_in_dormitory':
            logger.critical(f'[战斗] 舰队 {self.fleet} 的情绪控制设置为"保持开心加成"，且恢复地点设置为"港区"，两者不能同时使用，请检查情绪设置')
            raise RequestHumanTakeover
        # 在 14-4 使用双倍经验书时，预期情绪减少为 32，无法保持开心加成（>120）
        # 否则会导致无限任务延迟
        if self.control == 'keep_exp_bonus' and expected_reduce >= 29:
            expected_reduce = 29
            logger.info(f'[情绪-舰队] 舰队 {self.fleet} 预期扣减限制为29 '
                        f'当情绪控制="保持快乐奖励"时')

        emotion_needed = self.limit + expected_reduce - self.current
        if emotion_needed <= 0:
            return current_time()
        # speed 为每360秒的恢复量，换算恢复所需秒数
        seconds_needed = emotion_needed * 360 / self.speed
        return current_time() + timedelta(seconds=seconds_needed)

class Emotion:
    """情绪管理主类。

    编排舰队（和可选的共用心情槽位）的情绪追踪、等待和扣减。
    在战役开始前检查情绪是否足够，在战斗后扣减情绪值，
    并在情绪不足时延迟任务执行。

    普通模式下每个任务各记两支舰队的账（`Emotion.FleetN*`）。
    共用心情模式下改为按**实际舰队**记账（`PublicEmotion.FleetN*`）：
    同一支真实舰队被多个任务复用时共享同一份心情，不同舰队各自独立，
    因此参与任务可以用不同的舰队职能分工。

    Attributes:
        total_reduced (int): 本轮运行中累计扣减的情绪值，用于触发客户端 bug 重启。
        map_is_2x_book (bool): 是否使用二倍经验书（影响情绪扣减量）。
        fleet_1 (FleetEmotion): 第一舰队的情绪追踪器。
        fleet_2 (FleetEmotion): 第二舰队的情绪追踪器。
        sharing (dict): 真实舰队号 -> 情绪追踪器，空表示未启用共用心情。
        roles (dict): 真实舰队号 -> 逻辑编号（1 道中、2 Boss），共用心情模式下有效。
        fleets (list[FleetEmotion]): 参与本次情绪计算的舰队，供
            update()/record()/show() 统一遍历。
    """
    total_reduced = 0
    map_is_2x_book = False

    def __init__(self, config):
        """
        Args:
            config (AzurLaneConfig): 配置对象。
        """
        self.config = config
        self.fleet_1 = FleetEmotion(self.config, fleet=1)
        self.fleet_2 = FleetEmotion(self.config, fleet=2)
        self.roles = {}
        self.sharing = self._handle_public()
        if self.sharing:
            self.fleets = list(self.sharing.values())
        else:
            self.fleets = [self.fleet_1, self.fleet_2]

    def _handle_public(self):
        """判断本任务是否参与共用心情，并解析它用到哪几支真实舰队。

        真实舰队号取自 `Fleet.Fleet1` / `Fleet.Fleet2`（编队界面里的第几支舰队，
        1~6），由本任务的 `Fleet.FleetOrder` 决定谁打道中、谁打 Boss。
        `fleet_current_index` 那套 1 道中 / 2 Boss 只是逻辑编号，不参与记账。

        Returns:
            dict[int, FleetEmotion]: 真实舰队号 -> 情绪追踪器，空字典表示不启用共用心情。
        """
        if not getattr(self.config, 'PublicEmotion_Enable'):
            return {}

        tasks = getattr(self.config, 'PublicEmotion_Tasks')
        if not tasks:
            return {}

        tasks = [task.strip() for task in tasks.split(',')]
        if self.config.task.command not in tasks:
            return {}

        if not self.is_calculate:
            # 心情模式不含「计算心情消耗」时，wait()/reduce() 都不会被调用，
            # 于是这个任务打掉的心情完全不进共用账本，账本会偏高——在共用模式下
            # 偏高的账本会同时骗到其它共用同一支舰队的任务。
            logger.warning(f'[情绪-共用] 本任务的舰队 {self.config.Fleet_Fleet1}/{self.config.Fleet_Fleet2} '
                           f'已被共用心情监听，但心情模式不含「计算心情消耗」：'
                           f'它消耗的心情不会记进共用账本，请改成「计算心情消耗」或把它移出监控清单')

        numbers = real_fleets_of(
            self.config.Fleet_FleetOrder,
            self.config.Fleet_Fleet1,
            self.config.Fleet_Fleet2,
        )
        sharing = {
            number: FleetEmotion(self.config, fleet='Public', number=number)
            for number in numbers
        }
        self.roles = numbers
        logger.info(f'[情绪-共用] 本任务使用真实舰队 {numbers}')
        return sharing

    def _select(self, fleet_index):
        """按逻辑舰队编号取对应的真实舰队追踪器。

        Args:
            fleet_index (int): 逻辑舰队编号，1 道中 / 2 Boss。

        Returns:
            FleetEmotion | None: 对应的情绪追踪器，本次没用到该职能时返回 None。
        """
        if self.sharing:
            wanted = 1 if fleet_index == 1 else 2
            for number, role in self.roles.items():
                if role == wanted:
                    return self.sharing[number]
            return None
        return self.fleets[fleet_index - 1]

    @property
    def is_calculate(self):
        """是否在本任务里计算心情。

        任务级设置：被共用心情监听的任务在界面上被锁成会记账的两档，加进清单时也会
        自动掰回「计算心情消耗」，因此这里仍是"任务说了算"。
        """
        return 'calculate' in self.config.Emotion_Mode

    @property
    def is_ignore(self):
        """是否忽略红脸出击警告（照常出击，不做保底撤退与清零）。

        共用心情模式下这一项由**每支真实舰队**各自配置：任一本任务涉及的舰队要求
        无视就不打断任务——红脸弹窗是任务级的，多数调用点也拿不到"当前是哪支舰队"。
        """
        if self.sharing:
            return any(fleet.ignore_warning for fleet in self.sharing.values())
        return 'ignore' in self.config.Emotion_Mode

    def update(self):
        """更新情绪值。应在执行任何操作之前调用。"""
        for fleet in self.fleets:
            fleet.update()

    def record(self):
        """将当前情绪值保存到配置中。

        每次调用都更新 Value 和 Record 时间戳，确保不会因
        recovery + reduce 恰好使 value 不变时漏更新时间戳，
        导致下次 update() 从旧时间戳重复计算已消费的恢复量。

        Record 时间戳回扣 fractional_seconds 对应的等效秒数，
        使未满1点的恢复余数可在下次 update() 时继续累积。

        共用心情模式下还会按舰队职能把账本回写到本任务自己的
        `Emotion.FleetN*`（道中位 → Fleet1*，Boss 位 → Fleet2*）。这些槽位在共用
        模式下不参与记账（界面也已锁成只读），但任务被移出监控清单、回到普通记账时
        要拿它当基准：不回写的话它还是"进清单之前"的记录时间，恢复量会按这段离线
        时间一次性算出来，直接把心情顶到上限——偏高的账本方向不安全。
        覆盖不到"进了清单但一次都没跑过就移出"：那时任务槽位还是上次普通记账的值，
        会多算一段恢复量（有上限兜底，计算模式下红脸弹窗的 emergency_reset 也会拉回来）。

        注意：FleetEmotion.value 和 FleetEmotion.record 是 @property，
        从 self.config 实时读取。setattr 到 config 后属性自动更新，无需手动赋值。
        """
        with self.config.multi_set():
            for fleet in self.fleets:
                new_value = fleet.current
                record_time = current_time().replace(microsecond=0)
                fractional = fleet._fractional_seconds
                if fractional > 0:
                    # 回扣 fractional_seconds 对应的秒数
                    record_time = record_time - timedelta(seconds=fractional * 360 / fleet.speed)
                setattr(self.config, fleet.value_name, new_value)
                setattr(self.config, fleet.value_name.replace('Value', 'Record'), record_time)
                if self.sharing:
                    role = self.roles.get(fleet.number)
                    if role:
                        slot = self.fleet_1 if role == 1 else self.fleet_2
                        setattr(self.config, slot.value_name, new_value)
                        setattr(self.config, slot.value_name.replace('Value', 'Record'), record_time)

    def show(self):
        """显示当前计算的心情值（含时间恢复），而非上次保存值。"""
        for fleet in self.fleets:
            logger.attr(f'情绪真实舰队_{fleet.number}' if self.sharing
                        else f'情绪舰队_{fleet.fleet}', fleet.current)

    @property
    def reduce_per_battle(self):
        if self.map_is_2x_book:
            return 4
        else:
            return 2

    @property
    def reduce_per_battle_before_entering(self):
        if self.map_is_2x_book:
            return 4
        elif self.config.Campaign_Use2xBook:
            return 4
        else:
            return 2
    
    @property
    def reduce_shipwreck(self):
        return 10

    def _share_battles(self, battle):
        """按舰队职能把本次战役的战斗次数拆分到各逻辑舰队。

        Args:
            battle (int): 本次战役中的战斗次数。

        Returns:
            dict[int, int]: 逻辑编号（1 道中、2 Boss）-> 预期扣减量。
                单队全清时只有一项，且全部记在 1 上。
        """
        method = self.config.Fleet_FleetOrder

        if method == 'fleet1_mob_fleet2_boss':
            share = {1: battle - 1, 2: 1}
        elif method == 'fleet1_boss_fleet2_mob':
            share = {1: 1, 2: battle - 1}
        elif method == 'fleet1_all_fleet2_standby':
            share = {1: battle}
        elif method == 'fleet1_standby_fleet2_all':
            share = {2: battle}
        else:
            raise ScriptError(f'Unknown fleet order: {method}')

        rate = self.reduce_per_battle_before_entering
        return {role: count * rate for role, count in share.items()}

    def _check_reduce(self, battle):
        """检查战斗带来的情绪减少。

        预期扣减按舰队职能拆分，与实战中 `reduce()` 的扣减口径一致：
        道中队扣 `(战斗次数 - 1)` 场、Boss 队扣 1 场；单队全清时全部记在该队上。
        共用心情模式下拆分结果按真实舰队汇总，同一支真实舰队同时承担道中与
        Boss（单队全清）时扣减量相加。

        共用心情模式只判定本任务真正会出击的真实舰队（`self.sharing`），因此没编入
        出击位的舰队心情再低也不会拖住任务；普通模式沿用改造前的口径：两个槽位都
        参与判定，没有出击的槽位按"预计扣 0 点"算（这一处旧行为不在本次改动范围内）。

        Returns:
            recovered (datetime): 预期恢复时间。
            delay (bool): 是否需要延迟。
        """
        share = self._share_battles(battle)
        logger.info(f'[情绪-检查] 预期情绪扣减: {share}')

        self.update()
        self.record()
        self.show()

        if self.sharing:
            # 真实舰队号 -> 该舰队本次要扣的总量。
            expects = {}
            for number, role in self.roles.items():
                if role in share:
                    expects[number] = expects.get(number, 0) + share[role]
            recovered = max(
                fleet.get_recovered(expects.get(number, 0))
                for number, fleet in self.sharing.items()
            )
        else:
            expects = share
            recovered = max(
                fleet.get_recovered(expects.get(number, 0))
                for number, fleet in enumerate(self.fleets, start=1)
            )

        delay = recovered > current_time()
        return recovered, delay

    def check_reduce(self, battle):
        """进入战役前检查情绪。

        Args:
            battle (int): 本次战役中的战斗次数。

        Raise:
            ScriptEnd: 延迟当前任务以防止未来的情绪控制问题。
        """
        if not self.is_calculate:
            return

        recovered, delay = self._check_reduce(battle)
        if delay:
            logger.info('[情绪-延迟] 延迟当前任务以防止未来的情绪控制问题')
            self.config.task_delay(target=recovered)
            raise ScriptEnd('[情绪-延迟] 情绪控制')

    def wait(self, fleet_index):
        """等待指定舰队的情绪恢复。应在进入任何战斗之前调用。

        共用心情模式下等待的是 `fleet_index` 对应的**实际舰队**，
        而非所有参与任务的共同账本。

        Args:
            fleet_index (int): 逻辑舰队编号，1 或 2。
        """
        self.update()
        self.record()
        self.show()
        fleet = self._select(fleet_index)
        if fleet is None:
            # 本次出战没用到这个职能，不需要等。
            return

        recovered = fleet.get_recovered(expected_reduce=self.reduce_per_battle)
        if recovered > current_time():
            logger.hr('情绪等待')
            if self.sharing:
                logger.info(f'[情绪-等待] 真实舰队 {fleet.number} 情绪将恢复到 {fleet.limit}，时间 {recovered}')
            else:
                logger.info(f'[情绪-等待] 舰队 {fleet_index} 情绪将恢复到 {fleet.limit}，时间 {recovered}')

            while 1:
                if current_time() > recovered:
                    break

                logger.attr('等待直到', recovered)
                sleep(60)

    def reduce(self, fleet_index, shipwreck=False):
        """减少指定舰队的情绪值。应在战斗执行完成后调用。
        服务端在战斗加载完成后即扣减情绪。

        Args:
            fleet_index (int): 逻辑舰队编号，1 或 2。
            shipwreck (bool): 舰队是否遭遇船难。
        """
        logger.hr('情绪扣减')
        self.update()
        fleet = self._select(fleet_index)
        if fleet is None:
            # 本次出战没用到这个职能，没有心情可扣。
            return

        # 无视沉船心情惩罚：沉船的额外扣减发生在结算阶段，而进入战斗时
        # 已扣过基础扣减（reduce_per_battle），因此这里直接返回即可。
        # 同时不累加 total_reduced，让"无视"在情绪模型中完全等价于一场 S 评价。
        # 共用心情模式下这一项按**沉船的那支真实舰队**的设置，普通模式按任务级设置。
        if shipwreck and fleet.ignore_shipwreck:
            logger.info(f'[情绪-忽略] 真实舰队 {fleet.number} 已开启无视沉船心情惩罚，'
                        f'本次不额外扣减沉船心情')
            return

        if not shipwreck:
            fleet.current -= self.reduce_per_battle
            self.total_reduced += self.reduce_per_battle
        else:
            fleet.current -= self.reduce_shipwreck
            self.total_reduced += self.reduce_shipwreck
        self.record()
        self.show()

    def emergency_reset(self):
        """心情清零保底。计算模式下出现红脸弹窗时调用。

        将本任务用到的舰队心情值重置为0，强制下次任务等待心情恢复。
        这是计算模式下的异常保底措施，正常情况下不应被调用——
        计算模式会在进入战役前预检心情并延迟任务，红脸弹窗仅在
        ALAS计算错误或用户手动操作后才可能出现。

        重置内容：
        - FleetEmotion.current 设为 0
        - config 中的 Value 设为 0
        - config 中的 Record 设为当前时间（从0开始恢复计时）
        - _fractional_seconds 清零（丢弃未满1点的恢复余数）

        共用心情模式下只重置本任务用到的真实舰队，不波及其他任务共用的舰队。
        """
        with self.config.multi_set():
            for fleet in self.fleets:
                fleet.current = 0
                fleet._fractional_seconds = 0
                record_time = current_time().replace(microsecond=0)
                setattr(self.config, fleet.value_name, 0)
                setattr(self.config, fleet.value_name.replace('Value', 'Record'),
                        record_time)
        logger.info('[心情-保底] 已将所有舰队心情清零')

    @cached_property
    def bug_threshold(self):
        """
        Returns:
            int: 情绪 bug 触发阈值。
        """
        return random_normal_distribution_int(55, 105, n=2)

    def bug_threshold_reset(self):
        """情绪 bug 触发后调用此方法重置阈值。"""
        del self.__dict__['bug_threshold']

    def triggered_bug(self):
        """检测碧蓝航线客户端情绪计算 bug。
        客户端在长时间运行后无法正确计算情绪，需要重启游戏客户端使其更新。
        """
        logger.attr('情绪Bug', f'{self.total_reduced}/{self.bug_threshold}')
        if self.total_reduced >= self.bug_threshold:
            logger.info('[情绪-Bug] 碧蓝航线客户端未正确计算情绪，这是一个Bug。'
                        '长时间运行后，需要重启游戏客户端让客户端更新情绪。')
            self.total_reduced = 0
            self.bug_threshold_reset()
            return True
        else:
            return False
