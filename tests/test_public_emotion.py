"""共用心情按真实舰队记账的行为验证，不连接设备与真实配置目录。

覆盖三类行为：
- 任务设置里的真实舰队号 → 记账对象的映射（单队全清与两队分工）
- 预检时的战斗次数拆解，共用模式与普通模式口径一致
- 跨任务共享同一支真实舰队，不用到的舰队互不影响
- 旧单槽位配置迁移到按真实舰队拆分的字段
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from module.combat import emotion as emotion_module
from module.combat.emotion import Emotion, FleetEmotion, real_fleets_of
from module.config.deep import deep_get
from module.config.redirect_utils.utils import public_emotion_to_real_fleets_redirect, template_defaults
from module.config.time_source import now as current_time
from module.config.utils import read_file
from module.exception import RequestHumanTakeover, ScriptEnd


class Clock:
    """合成时钟：按需推进，不依赖真实流逝与 NTP 校准。

    模块里的 `current_time()` 是校准过的时间、两次校准之间还是冻结的，直接 sleep
    既慢又不收敛；这里把它换成可手动推进的实现，用来构造"打一场、过一会儿、再打"
    这种消耗与恢复交替的场景，以及"关掉程序一段时间"的离线恢复。
    """

    def __init__(self, start=None):
        self.now = start or current_time()
        self._patch = None

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)
        return self.now

    def __enter__(self):
        self._patch = mock.patch.object(emotion_module, 'current_time', lambda: self.now)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class FakeTask:
    """替身任务对象，只需暴露 command 供共用心情名单比对。"""

    def __init__(self, command):
        self.command = command


class FakeConfig:
    """最小配置替身，按属性名提供与 AzurLaneConfig 相同的访问方式。

    只实现 Emotion 用到的那部分：属性读写落到字典，multi_set 为无操作上下文。
    """

    def __init__(self, **kwargs):
        tasks = kwargs.pop('tasks', None)
        object.__setattr__(self, 'task', FakeTask(kwargs.pop('task_command', 'Main')))
        object.__setattr__(self, '_values', kwargs)
        object.__setattr__(self, 'bound', {})
        if tasks is not None:
            object.__setattr__(self, 'tasks', tasks)

    def __getattr__(self, item):
        try:
            return self._values[item]
        except KeyError:
            raise AttributeError(item) from None

    def __setattr__(self, key, value):
        self._values[key] = value

    class _MultiSet:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def multi_set(self):
        return self._MultiSet()

    def set_fleet(self, fleet_1, fleet_2=0, order='fleet1_mob_fleet2_boss'):
        """设定本任务的出击舰队与职能分工。"""
        self._values['Fleet_Fleet1'] = fleet_1
        self._values['Fleet_Fleet2'] = fleet_2
        self._values['Fleet_FleetOrder'] = order
        return self

    def set_morale(self, number, value, record=None):
        """把某支真实舰队设为"刚结算过"的状态，让后续扣减从确定值开始。"""
        self._values[f'PublicEmotion_Fleet{number}Value'] = value
        self._values[f'PublicEmotion_Fleet{number}Record'] = record or datetime.now()
        return self

    def set_task_morale(self, fleet, value, record=None):
        """普通模式：把任务自己的 `Emotion.FleetN*` 设为"刚结算过"的状态。"""
        self._values[f'Emotion_Fleet{fleet}Value'] = value
        self._values[f'Emotion_Fleet{fleet}Record'] = record or datetime.now()
        return self

    def set_fleet_flags(self, number, warning=False, shipwreck=False):
        """共用模式：设定某支真实舰队的两个开关（无视红脸警告 / 无视沉船惩罚）。"""
        self._values[f'PublicEmotion_Fleet{number}IgnoreWarning'] = warning
        self._values[f'PublicEmotion_Fleet{number}IgnoreShipwreck'] = shipwreck
        return self


def base_config(**overrides):
    """构造一份可用的共用心情配置：Main 使用真实舰队 1（道中）+ 2（Boss）。"""
    values = {
        'Emotion_Mode': 'calculate',
        'Campaign_Use2xBook': False,
        'Emotion_IgnoreShipwreck': False,
        'Fleet_FleetOrder': 'fleet1_mob_fleet2_boss',
        'Fleet_Fleet1': 1,
        'Fleet_Fleet2': 2,
        'PublicEmotion_Enable': True,
        'PublicEmotion_Tasks': 'Main, Event',
        'task_command': 'Main',
    }
    for number in range(1, 7):
        values[f'PublicEmotion_Fleet{number}Value'] = 119
        values[f'PublicEmotion_Fleet{number}Record'] = datetime(2020, 1, 1)
        values[f'PublicEmotion_Fleet{number}Control'] = 'prevent_yellow_face'
        values[f'PublicEmotion_Fleet{number}Recover'] = 'dormitory_floor_2'
        values[f'PublicEmotion_Fleet{number}Oath'] = False
        values[f'PublicEmotion_Fleet{number}Onsen'] = False
        # 「无视红脸出击警告」「无视沉船心情惩罚」在共用模式下按真实舰队各自配置。
        values[f'PublicEmotion_Fleet{number}IgnoreWarning'] = False
        values[f'PublicEmotion_Fleet{number}IgnoreShipwreck'] = False
    # 普通模式的字段，共用模式下不应被读写。
    for fleet in ('1', '2'):
        values[f'Emotion_Fleet{fleet}Value'] = 119
        values[f'Emotion_Fleet{fleet}Record'] = datetime(2020, 1, 1)
        values[f'Emotion_Fleet{fleet}Control'] = 'prevent_yellow_face'
        values[f'Emotion_Fleet{fleet}Recover'] = 'dormitory_floor_2'
        values[f'Emotion_Fleet{fleet}Oath'] = False
        values[f'Emotion_Fleet{fleet}Onsen'] = False
    values.update(overrides)
    return FakeConfig(**values)


class RealFleetTests(unittest.TestCase):
    """real_fleets_of 把任务配置翻译成真实舰队号与职能。"""

    def test_two_fleet_split_uses_both_numbers(self):
        self.assertEqual({4: 1, 3: 2}, real_fleets_of('fleet1_mob_fleet2_boss', 4, 3))

    def test_reversed_roles_keep_real_numbers(self):
        """职能互换不改变真实舰队号，只是谁打道中不同。"""
        self.assertEqual({3: 1, 4: 2}, real_fleets_of('fleet1_boss_fleet2_mob', 3, 4))

    def test_single_fleet_clear_uses_first_slot(self):
        self.assertEqual({1: 1}, real_fleets_of('fleet1_all_fleet2_standby', 1, 2))

    def test_single_fleet_clear_on_second_slot(self):
        self.assertEqual({2: 2}, real_fleets_of('fleet1_standby_fleet2_all', 1, 2))

    def test_fleet_two_disabled_falls_back_to_one(self):
        """Fleet2 为 0 表示只带一支，职能声明的 Boss 位不成立。"""
        self.assertEqual({1: 1}, real_fleets_of('fleet1_mob_fleet2_boss', 1, 0))

    def test_same_number_twice_collapses_to_one(self):
        """同一支舰队占两个出击位时只记一份账。"""
        self.assertEqual({5: 1}, real_fleets_of('fleet1_mob_fleet2_boss', 5, 5))

    def test_no_fleet_configured(self):
        self.assertEqual({}, real_fleets_of('fleet1_mob_fleet2_boss', 0, 0))


class MappingTests(unittest.TestCase):
    """逻辑编号 → 真实舰队的解析。"""

    def test_disabled_keeps_normal_mode(self):
        emotion = Emotion(base_config(PublicEmotion_Enable=False))
        self.assertEqual({}, emotion.sharing)
        self.assertEqual([1, 2], [fleet.fleet for fleet in emotion.fleets])

    def test_task_outside_list_keeps_normal_mode(self):
        """只有名单内的任务参与共用心情。"""
        emotion = Emotion(base_config(task_command='OpsiExplore'))
        self.assertEqual({}, emotion.sharing)

    def test_empty_task_list_keeps_normal_mode(self):
        emotion = Emotion(base_config(PublicEmotion_Tasks=None))
        self.assertEqual({}, emotion.sharing)

    def test_two_fleet_task_shares_both_real_fleets(self):
        emotion = Emotion(base_config().set_fleet(4, 3))
        self.assertEqual({4: 1, 3: 2}, emotion.roles)
        self.assertEqual([3, 4], sorted(emotion.sharing))
        self.assertEqual([3, 4], sorted(fleet.number for fleet in emotion.fleets))

    def test_single_fleet_task_shares_one_real_fleet(self):
        """单队全清只占一支，不会凭空扣减没出击的舰队。"""
        emotion = Emotion(base_config().set_fleet(5, 0, 'fleet1_all_fleet2_standby'))
        self.assertEqual({5: 1}, emotion.roles)
        self.assertEqual([5], sorted(emotion.sharing))

    def test_select_maps_role_to_real_fleet(self):
        emotion = Emotion(base_config().set_fleet(4, 3))
        self.assertEqual(4, emotion._select(1).number)
        self.assertEqual(3, emotion._select(2).number)

    def test_select_maps_boss_role_when_reversed(self):
        emotion = Emotion(base_config().set_fleet(6, 2, 'fleet1_boss_fleet2_mob'))
        self.assertEqual(6, emotion._select(1).number)
        self.assertEqual(2, emotion._select(2).number)

    def test_select_returns_none_for_unused_role(self):
        """没出击的职能返回 None，调用方据此跳过等待与扣减。"""
        emotion = Emotion(base_config().set_fleet(5, 0, 'fleet1_all_fleet2_standby'))
        self.assertEqual(5, emotion._select(1).number)
        self.assertIsNone(emotion._select(2))


class ShareBattleTests(unittest.TestCase):
    """_share_battles 把战斗次数拆到逻辑职能上。"""

    def test_single_fleet_clear_all_on_mob_role(self):
        emotion = Emotion(base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby'))
        self.assertEqual({1: 18}, emotion._share_battles(9))

    def test_single_fleet_clear_on_second_slot_uses_boss_role(self):
        emotion = Emotion(base_config().set_fleet(2, 0, 'fleet1_standby_fleet2_all'))
        self.assertEqual({2: 18}, emotion._share_battles(9))

    def test_mob_boss_split(self):
        emotion = Emotion(base_config())
        self.assertEqual({1: 16, 2: 2}, emotion._share_battles(9))

    def test_boss_mob_split_is_reversed(self):
        emotion = Emotion(base_config(Fleet_FleetOrder='fleet1_boss_fleet2_mob'))
        self.assertEqual({1: 2, 2: 16}, emotion._share_battles(9))

    def test_unknown_order_raises(self):
        from module.exception import ScriptError

        emotion = Emotion(base_config())
        emotion.config.Fleet_FleetOrder = 'unknown_order'
        with self.assertRaises(ScriptError):
            emotion._share_battles(9)

    def test_after_entering_counts_double_book_per_battle(self):
        """开了二倍经验书后每场扣 4 点，预检同样按 4 点算。"""
        emotion = Emotion(base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby'))
        emotion.map_is_2x_book = True
        self.assertEqual({1: 12}, emotion._share_battles(3))

    def test_before_entering_counts_double_book_from_config(self):
        """还没开书但配置了二倍书：预检先按 4 点保守估计，实战开书前仍是 2 点。"""
        emotion = Emotion(base_config(Campaign_Use2xBook=True).set_fleet(1, 0, 'fleet1_all_fleet2_standby'))
        self.assertEqual({1: 12}, emotion._share_battles(3))
        self.assertEqual(2, emotion.reduce_per_battle)


class DeductionTests(unittest.TestCase):
    """扣减只落在对应的真实舰队上。"""

    def test_reduce_touches_role_fleet_only(self):
        config = base_config().set_fleet(4, 3).set_morale(4, 119).set_morale(3, 119)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1)
        self.assertEqual(117, config.PublicEmotion_Fleet4Value)
        self.assertEqual(119, config.PublicEmotion_Fleet3Value)

    def test_reduce_boss_role_hits_other_fleet(self):
        config = base_config().set_fleet(4, 3).set_morale(4, 119).set_morale(3, 119)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=2)
        self.assertEqual(119, config.PublicEmotion_Fleet4Value)
        self.assertEqual(117, config.PublicEmotion_Fleet3Value)

    def test_reduce_on_unused_role_is_noop(self):
        """单队全清时 Boss 职能没出击，不应扣任何舰队的心情。"""
        config = base_config().set_fleet(5, 0, 'fleet1_all_fleet2_standby').set_morale(5, 119)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=2)
        self.assertEqual(119, config.PublicEmotion_Fleet5Value)

    def test_normal_mode_writes_emotion_fields(self):
        """未启用共用心情时仍读写普通模式字段。"""
        config = base_config(PublicEmotion_Enable=False)
        for fleet in ('1', '2'):
            config._values[f'Emotion_Fleet{fleet}Value'] = 119
            config._values[f'Emotion_Fleet{fleet}Record'] = datetime.now()
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1)
        self.assertEqual(117, config.Emotion_Fleet1Value)
        self.assertEqual(119, config.Emotion_Fleet2Value)

    def test_check_reduce_ignores_unused_fleet(self):
        """单队全清预检时，没出击的舰队心情再低也不该触发延迟。"""
        config = base_config().set_fleet(5, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(5, 119)
        # 其它真实舰队心情极低，若被误算会立刻触发延迟。
        for number in (1, 2, 3, 4, 6):
            config.set_morale(number, 0, record=datetime.now() - timedelta(days=1))
        emotion = Emotion(config)
        recovered, delay = emotion._check_reduce(9)
        self.assertFalse(delay, f'没出击的舰队不应触发延迟，recovered={recovered}')

    def test_check_reduce_charges_single_fleet_both_roles(self):
        """单队全清的道中队同时承担道中与 Boss，扣减量相加。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 119)
        emotion = Emotion(config)
        # 9 场 → 道中 8 场 + Boss 1 场 = 9 场 × 2 = 18，目标阈值 30。
        recovered, _delay = emotion._check_reduce(9)
        self.assertGreater(recovered, datetime.now())

    def test_campaign_total_matches_precheck_single_fleet(self):
        """实战逐场扣减的总量，与战前预检算出的量一致（单队全清）。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 130, record=current_time())
        emotion = Emotion(config)
        expects = emotion._share_battles(6)
        self.assertEqual({1: 12}, expects)
        for _ in range(6):
            emotion.reduce(fleet_index=1)
        self.assertEqual(130 - expects[1], config.PublicEmotion_Fleet1Value)
        self.assertEqual(12, emotion.total_reduced)

    def test_campaign_total_matches_precheck_two_fleets(self):
        """两队分工：道中 5 场、Boss 1 场，各扣各的真实舰队。"""
        config = base_config().set_fleet(4, 3)
        config.set_morale(4, 130, record=current_time()).set_morale(3, 130, record=current_time())
        emotion = Emotion(config)
        self.assertEqual({1: 10, 2: 2}, emotion._share_battles(6))
        for _ in range(5):
            emotion.reduce(fleet_index=1)
        emotion.reduce(fleet_index=2)
        self.assertEqual(120, config.PublicEmotion_Fleet4Value)
        self.assertEqual(128, config.PublicEmotion_Fleet3Value)
        self.assertEqual(12, emotion.total_reduced)

    def test_single_fleet_clear_on_second_slot_charges_that_fleet(self):
        """单队全清在第二槽位时，全部扣在那支舰队上，道中位没有账可扣。"""
        config = base_config().set_fleet(0, 2, 'fleet1_standby_fleet2_all')
        config.set_morale(2, 130, record=current_time())
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1)
        self.assertEqual(130, config.PublicEmotion_Fleet2Value)
        for _ in range(6):
            emotion.reduce(fleet_index=2)
        self.assertEqual(118, config.PublicEmotion_Fleet2Value)

    def test_double_book_deducts_four_per_battle(self):
        """开书后单场 4 点：同样 6 场扣 24 点，累计量也要跟上。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 130, record=current_time())
        emotion = Emotion(config)
        emotion.map_is_2x_book = True
        for _ in range(6):
            emotion.reduce(fleet_index=1)
        self.assertEqual(106, config.PublicEmotion_Fleet1Value)
        self.assertEqual(24, emotion.total_reduced)

    def test_shipwreck_charges_role_fleet(self):
        """Boss 队沉船只扣 Boss 那支真实舰队，道中队不受影响。"""
        config = base_config().set_fleet(4, 3)
        config.set_morale(4, 130, record=current_time()).set_morale(3, 130, record=current_time())
        emotion = Emotion(config)
        emotion.reduce(fleet_index=2, shipwreck=True)
        self.assertEqual(130, config.PublicEmotion_Fleet4Value)
        self.assertEqual(120, config.PublicEmotion_Fleet3Value)
        self.assertEqual(emotion.reduce_shipwreck, emotion.total_reduced)

    def test_recover_settings_are_per_real_fleet(self):
        """每支真实舰队按自己的恢复地点/誓约/温泉恢复，不互相借用。"""
        config = base_config().set_fleet(4, 3)
        config.set_morale(4, 50, record=current_time() - timedelta(hours=1))
        config.set_morale(3, 50, record=current_time() - timedelta(hours=1))
        config.PublicEmotion_Fleet4Recover = 'dormitory_floor_2'
        config.PublicEmotion_Fleet4Oath = True     # (50 + 10) // 10 = 6 点/360 秒 → 60 点/小时
        config.PublicEmotion_Fleet3Recover = 'not_in_dormitory'
        config.PublicEmotion_Fleet3Oath = False    # 2 点/360 秒 → 20 点/小时
        emotion = Emotion(config)
        emotion.update()
        self.assertEqual(110, emotion.sharing[4].current)
        self.assertEqual(70, emotion.sharing[3].current)

    def test_onsen_and_oath_add_to_recovery(self):
        """温泉与誓约各加 10 点/小时的恢复速度。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.PublicEmotion_Fleet1Recover = 'dormitory_floor_1'
        config.PublicEmotion_Fleet1Onsen = True
        emotion = Emotion(config)
        # (40 + 10) // 10 = 5 点/360 秒。
        self.assertEqual(5, emotion.sharing[1].speed)

    def test_monitored_task_without_fleet_config_falls_back(self):
        """名单内但没把舰队编进出击位时，退回任务自己的账（不会半共用）。"""
        emotion = Emotion(base_config().set_fleet(0, 0))
        self.assertEqual({}, emotion.sharing)
        self.assertEqual({}, emotion.roles)
        self.assertEqual([1, 2], [fleet.fleet for fleet in emotion.fleets])

    def test_check_reduce_delays_on_the_fleet_that_runs_low(self):
        """两队分工时，按真正不够的那支舰队算延迟时间。"""
        config = base_config().set_fleet(4, 3)
        config.set_morale(4, 119, record=current_time())
        config.set_morale(3, 10, record=current_time())
        emotion = Emotion(config)
        recovered, delay = emotion._check_reduce(6)
        self.assertTrue(delay)
        # Boss 舰队 10 点、阈值 30、预计扣 2 点：需恢复 22 点 ÷ 50 点/小时 ≈ 26 分钟。
        expected = current_time() + timedelta(seconds=22 * 360 / 5)
        self.assertLess(abs((recovered - expected).total_seconds()), 60)


class CrossTaskTests(unittest.TestCase):
    """跨任务共享同一支真实舰队。"""

    def test_two_tasks_using_same_real_fleet_share(self):
        """两个任务都用真实舰队 4 时，第二个任务接着第一个任务的账继续扣。"""
        config_a = base_config(task_command='Main').set_fleet(4, 0, 'fleet1_all_fleet2_standby')
        config_a.set_morale(4, 119)
        emotion_a = Emotion(config_a)
        emotion_a.reduce(fleet_index=1)
        self.assertEqual(117, config_a.PublicEmotion_Fleet4Value)

        config_b = base_config(task_command='Event').set_fleet(4, 0, 'fleet1_all_fleet2_standby')
        config_b.set_morale(4, config_a.PublicEmotion_Fleet4Value,
                            record=config_a.PublicEmotion_Fleet4Record)
        emotion_b = Emotion(config_b)
        emotion_b.reduce(fleet_index=1)
        self.assertEqual(115, config_b.PublicEmotion_Fleet4Value)

    def test_tasks_using_different_real_fleets_are_independent(self):
        """用不同真实舰队的两个任务互不影响。"""
        config_a = base_config(task_command='Main').set_fleet(4, 0, 'fleet1_all_fleet2_standby')
        config_a.set_morale(4, 119).set_morale(3, 119)
        Emotion(config_a).reduce(fleet_index=1)
        self.assertEqual(117, config_a.PublicEmotion_Fleet4Value)
        self.assertEqual(119, config_a.PublicEmotion_Fleet3Value)

        config_b = base_config(task_command='Event').set_fleet(3, 0, 'fleet1_all_fleet2_standby')
        config_b.set_morale(4, 119).set_morale(3, 119)
        Emotion(config_b).reduce(fleet_index=1)
        self.assertEqual(119, config_b.PublicEmotion_Fleet4Value)
        self.assertEqual(117, config_b.PublicEmotion_Fleet3Value)

    def test_heterogeneous_tasks_coexist(self):
        """一个任务单队全清、另一个两队分工，各记各的真实舰队。"""
        single = Emotion(base_config(task_command='Main').set_fleet(5, 0, 'fleet1_all_fleet2_standby'))
        pair = Emotion(base_config(task_command='Event').set_fleet(4, 3))
        self.assertEqual({5: 1}, single.roles)
        self.assertEqual({4: 1, 3: 2}, pair.roles)
        self.assertEqual({1: 18}, single._share_battles(9))
        self.assertEqual({1: 16, 2: 2}, pair._share_battles(9))

    def test_second_task_continues_from_first_task_ledger(self):
        """任务1 打完一整场战役后，任务2 接着这本账继续扣（不重复扣、不各扣一份）。"""
        config = base_config(task_command='Main').set_fleet(4, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(4, 130, record=current_time())
        emotion_a = Emotion(config)
        for _ in range(6):
            emotion_a.reduce(fleet_index=1)
        self.assertEqual(118, config.PublicEmotion_Fleet4Value)

        # 同一个配置文件、换成任务2 跑：解析出来的还是舰队4 这本账。
        config.task.command = 'Event'
        emotion_b = Emotion(config)
        emotion_b.update()
        self.assertEqual(118, emotion_b.sharing[4].current)
        recovered, delay = emotion_b._check_reduce(6)
        self.assertFalse(delay, f'118 点够再打 6 场，不该延迟，recovered={recovered}')
        for _ in range(6):
            emotion_b.reduce(fleet_index=1)
        self.assertEqual(106, config.PublicEmotion_Fleet4Value)

    def test_second_task_delays_because_first_consumed(self):
        """共用账本被前一个任务消耗到阈值附近后，后一个任务会因此延迟。"""
        config = base_config(task_command='Main').set_fleet(4, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(4, 46, record=current_time())
        config.task_delay = mock.Mock()
        emotion_a = Emotion(config)
        recovered, delay = emotion_a._check_reduce(6)
        self.assertFalse(delay, f'46 点刚好够自己这场，recovered={recovered}')
        for _ in range(6):
            emotion_a.reduce(fleet_index=1)
        self.assertEqual(34, config.PublicEmotion_Fleet4Value)

        # 换任务2：同一本账只剩 34 点，再打 6 场会跌破阈值 30 → 延迟整个任务。
        config.task.command = 'Event'
        config.task_delay = mock.Mock()
        with self.assertRaises(ScriptEnd):
            Emotion(config).check_reduce(6)
        config.task_delay.assert_called_once()

    def test_low_shared_ledger_delays_before_any_battle(self):
        """共用账本本身不够时，任务在战役前就被延迟。"""
        config = base_config(task_command='Main').set_fleet(4, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(4, 35, record=current_time())
        config.task_delay = mock.Mock()
        with self.assertRaises(ScriptEnd):
            Emotion(config).check_reduce(6)
        config.task_delay.assert_called_once()


class FieldAccessTests(unittest.TestCase):
    """字段按真实舰队号读写。"""

    def test_public_fleet_reads_numbered_keys(self):
        config = base_config()
        config.PublicEmotion_Fleet3Value = 42
        fleet = FleetEmotion(config, fleet='Public', number=3)
        self.assertEqual('PublicEmotion_Fleet3Value', fleet.value_name)
        self.assertEqual(42, fleet.value)

    def test_normal_fleet_keeps_legacy_key_shape(self):
        """普通模式不受影响，仍是 Emotion.FleetN*。"""
        config = base_config()
        fleet = FleetEmotion(config, fleet='1')
        self.assertEqual('Emotion_Fleet1Value', fleet.value_name)
        self.assertEqual(1, fleet.number)

    def test_second_slot_never_reads_legacy(self):
        """旧键不再参与读取，缺少编号字段时直接报错而不是串号。"""
        config = base_config()
        del config._values['PublicEmotion_Fleet2Value']
        fleet = FleetEmotion(config, fleet='Public', number=2)
        with self.assertRaises(AttributeError):
            _ = fleet.value


class LegacySlotRedirectTests(unittest.TestCase):
    """旧单槽位配置迁移到按真实舰队拆分的字段。

    迁移条件要同时看两边：老字段还在用户文件里（可能要迁），目标字段还停在模板
    默认值（说明确实没被接管过）。只看一半都会错——只看老字段，配置里删不掉的
    残留老键会让迁移每次加载都重跑，把脚本已经记上账的值顶回旧值。
    """

    BASE = 'General.PublicEmotion'
    SLOTS = ('Value', 'Record', 'Control', 'Recover', 'Oath', 'Onsen')

    def default(self, argument):
        return deep_get(template_defaults(), f'{self.BASE}.{argument}.value')

    def build(self, **overrides):
        """目标字段全取模板默认值，再叠加测试关心的那几项。"""
        node = {f'Fleet1{suffix}': self.default(f'Fleet1{suffix}') for suffix in self.SLOTS}
        node.update(overrides)
        return {'General': {'PublicEmotion': node}}

    def legacy(self, **overrides):
        node = {'FleetValue': 0, 'FleetRecord': '2026-09-19 19:50:35'}
        node.update(overrides)
        return {'General': {'PublicEmotion': node}}

    def test_migrates_legacy_slot_into_fleet_1(self):
        out = public_emotion_to_real_fleets_redirect(self.build(), self.legacy())
        self.assertEqual(0, deep_get(out, f'{self.BASE}.Fleet1Value'))
        self.assertEqual('2026-09-19 19:50:35', deep_get(out, f'{self.BASE}.Fleet1Record'))
        # 旧配置表达不出对应哪支舰队，其余舰队一个字段都不碰。
        self.assertIsNone(deep_get(out, f'{self.BASE}.Fleet2Value'))

    def test_accounted_target_is_not_overwritten(self):
        """目标字段已经被记账过就不再迁移。"""
        new = self.build(Fleet1Value=100, Fleet1Record='2026-09-21 00:35:06')
        out = public_emotion_to_real_fleets_redirect(new, self.legacy())
        self.assertEqual(100, deep_get(out, f'{self.BASE}.Fleet1Value'))
        self.assertEqual('2026-09-21 00:35:06', deep_get(out, f'{self.BASE}.Fleet1Record'))

    def test_migrates_only_fields_still_at_default(self):
        """逐字段判断：已记账的保留，还没动过的照迁。"""
        new = self.build(Fleet1Value=100)
        out = public_emotion_to_real_fleets_redirect(new, self.legacy(FleetOnsen=True))
        self.assertEqual(100, deep_get(out, f'{self.BASE}.Fleet1Value'))
        self.assertIs(True, deep_get(out, f'{self.BASE}.Fleet1Onsen'))

    def test_untouched_config_keeps_defaults(self):
        old = {'General': {'PublicEmotion': {'Enable': False}}}
        out = public_emotion_to_real_fleets_redirect(self.build(), old)
        self.assertEqual(self.default('Fleet1Value'), deep_get(out, f'{self.BASE}.Fleet1Value'))
        self.assertEqual(self.default('Fleet1Record'), deep_get(out, f'{self.BASE}.Fleet1Record'))


class FleetFlagTests(unittest.TestCase):
    """「无视红脸出击警告」「无视沉船心情惩罚」在共用模式下按真实舰队取值。"""

    def test_ignore_warning_follows_any_involved_fleet(self):
        """任一涉及舰队要求无视就不打断任务（红脸弹窗是任务级的，拿不到是哪支舰队）。"""
        config = base_config().set_fleet(4, 3)
        config.set_fleet_flags(4, warning=True)
        emotion = Emotion(config)
        self.assertTrue(emotion.is_ignore)
        self.assertEqual({4: True, 3: False},
                         {number: fleet.ignore_warning for number, fleet in emotion.sharing.items()})

    def test_ignore_warning_false_when_no_fleet_asks(self):
        emotion = Emotion(base_config().set_fleet(4, 3))
        self.assertFalse(emotion.is_ignore)

    def test_shared_mode_still_follows_task_mode(self):
        """是否记账仍由任务自己决定，舰队级开关只管"无视红脸警告/沉船惩罚"。

        界面上被监听的任务锁成会记账的两档、加进清单时也会自动掰回，这里只是
        说明后端没有偷偷改口径——真被写成不记账，_handle_public 会打一条 warning。
        """
        config = base_config(Emotion_Mode='ignore').set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        emotion = Emotion(config)
        self.assertFalse(emotion.is_calculate)
        self.assertEqual({1}, set(emotion.sharing))

    def test_normal_mode_still_follows_task_mode(self):
        """普通模式没变：仍然只看任务自己的心情模式。"""
        self.assertFalse(Emotion(base_config(Emotion_Mode='ignore', PublicEmotion_Enable=False)).is_calculate)
        self.assertTrue(Emotion(base_config(Emotion_Mode='ignore', PublicEmotion_Enable=False)).is_ignore)

    def test_shipwreck_penalty_follows_that_fleet(self):
        """只有开了开关的那支真实舰队不扣沉船惩罚，另一支照扣。"""
        config = base_config().set_fleet(4, 3)
        config.set_morale(4, 130, record=current_time()).set_morale(3, 130, record=current_time())
        config.set_fleet_flags(4, shipwreck=True)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1, shipwreck=True)
        self.assertEqual(130, config.PublicEmotion_Fleet4Value)
        emotion.reduce(fleet_index=2, shipwreck=True)
        self.assertEqual(120, config.PublicEmotion_Fleet3Value)

    def test_normal_mode_shipwreck_still_follows_task(self):
        config = base_config(PublicEmotion_Enable=False, Emotion_IgnoreShipwreck=True)
        config.set_task_morale('1', 130)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1, shipwreck=True)
        self.assertEqual(130, config.Emotion_Fleet1Value)
        self.assertEqual(0, emotion.total_reduced)


class NormalModeTests(unittest.TestCase):
    """不监听时的原有逻辑：按任务自己的 `Emotion.Fleet1/2*` 记账。

    共用心情是可选叠加：开关关掉、任务不在名单里、名单内但没配出击舰队时，都必须
    退回改造前的行为——两个**逻辑槽位**各记各的账，与共用账本、与真实舰队号无关。
    这一组专门盯住它，免得按真实舰队重构时把没启用共用心情的任务一起改坏。
    """

    def normal(self, **overrides):
        """普通模式配置：任务自己的两个槽位都从 130 点起。"""
        config = base_config(PublicEmotion_Enable=False, **overrides)
        for fleet in ('1', '2'):
            config.set_task_morale(fleet, 130)
        return config

    def test_select_keeps_logical_slots(self):
        """普通模式按槽位取舰队，与出击编制、真实舰队号都无关。"""
        config = self.normal()
        config.set_fleet(4, 3)
        emotion = Emotion(config)
        self.assertEqual(1, emotion._select(1).fleet)
        self.assertEqual(2, emotion._select(2).fleet)
        self.assertEqual('Emotion_Fleet1Value', emotion._select(1).value_name)

    def test_deduction_stays_on_task_ledger(self):
        """扣减只落在任务自己的字段上，共用账本一个字段都不该动。"""
        config = self.normal()
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1)
        self.assertEqual(128, config.Emotion_Fleet1Value)
        self.assertEqual(130, config.Emotion_Fleet2Value)
        self.assertEqual(119, config.PublicEmotion_Fleet1Value)
        self.assertEqual(119, config.PublicEmotion_Fleet2Value)
        self.assertEqual(2, emotion.total_reduced)

    def test_campaign_total_matches_precheck(self):
        """两队分工 6 场：道中位扣 10 点、Boss 位扣 2 点，与预检拆分一致。"""
        config = self.normal()
        emotion = Emotion(config)
        self.assertEqual({1: 10, 2: 2}, emotion._share_battles(6))
        for _ in range(5):
            emotion.reduce(fleet_index=1)
        emotion.reduce(fleet_index=2)
        self.assertEqual(120, config.Emotion_Fleet1Value)
        self.assertEqual(128, config.Emotion_Fleet2Value)
        self.assertEqual(12, emotion.total_reduced)

    def test_single_fleet_clear_deducts_first_slot(self):
        """单队全清时全部扣在第一槽位，第二槽位不动。"""
        config = self.normal()
        config.set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        emotion = Emotion(config)
        for _ in range(6):
            emotion.reduce(fleet_index=1)
        self.assertEqual(118, config.Emotion_Fleet1Value)
        self.assertEqual(130, config.Emotion_Fleet2Value)

    def test_check_reduce_reads_task_ledger(self):
        """预检看任务自己的心情：任务账低就延迟，共用账再高也不顶用。"""
        config = self.normal()
        config.set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_task_morale('1', 10)
        for number in range(1, 7):
            config.set_morale(number, 150)
        config.task_delay = mock.Mock()
        with self.assertRaises(ScriptEnd):
            Emotion(config).check_reduce(1)
        config.task_delay.assert_called_once()

    def test_check_reduce_ignores_shared_ledger(self):
        """反过来：共用账本全是 0 也不该影响没被监听的任务。"""
        config = self.normal()
        for number in range(1, 7):
            config.set_morale(number, 0, record=current_time() - timedelta(days=1))
        config.task_delay = mock.Mock()
        Emotion(config).check_reduce(9)
        config.task_delay.assert_not_called()

    def test_check_reduce_keeps_idle_slot_behavior(self):
        """单队全清时二队没出击，但预检仍按改造前的口径算它一次。

        这是改造前就有的行为（待命槽位心情见底会把任务拖住），不属于本次共用心情
        重构的范围，因此原样保留：普通模式必须与改造前逐值一致。共用心情模式下
        不会这样——那边只判定本任务真正会出击的真实舰队。
        """
        config = self.normal()
        config.set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_task_morale('1', 119)
        config.set_task_morale('2', 0)  # 二队没出击、心情为 0
        config.task_delay = mock.Mock()
        with self.assertRaises(ScriptEnd):
            Emotion(config).check_reduce(6)
        config.task_delay.assert_called_once()

    def test_check_reduce_still_checks_both_working_fleets(self):
        """两队都出击时，任何一支不够都要延迟（别把 skip 用过头）。"""
        config = self.normal()
        config.set_fleet(4, 3)
        config.set_task_morale('1', 119)
        config.set_task_morale('2', 10)  # Boss 队心情不够
        config.task_delay = mock.Mock()
        with self.assertRaises(ScriptEnd):
            Emotion(config).check_reduce(6)
        config.task_delay.assert_called_once()

    def test_recovery_uses_task_record(self):
        """恢复按任务自己的 Record 推算（后宅二层 50 点/小时）。"""
        config = self.normal()
        config.set_task_morale('1', 10, record=current_time() - timedelta(hours=1))
        emotion = Emotion(config)
        emotion.update()
        self.assertEqual(60, emotion.fleet_1.current)
        self.assertEqual(50, emotion.fleet_1.speed * 10)  # speed=5 点/360 秒 = 50 点/小时
        emotion.record()
        self.assertGreater(config.Emotion_Fleet1Record, current_time() - timedelta(seconds=5))

    def test_two_unmonitored_tasks_keep_separate_ledgers(self):
        """两个都没被监听的任务各记各的账，同名槽位不串。"""
        main = self.normal(task_command='Main')
        event = self.normal(task_command='Event')
        Emotion(main).reduce(fleet_index=1)
        self.assertEqual(128, main.Emotion_Fleet1Value)
        self.assertEqual(130, event.Emotion_Fleet1Value)

    def test_out_of_list_and_disabled_behave_the_same(self):
        """开关关掉、任务不在名单里：落点都是任务自己的账。"""
        configs = (
            base_config(PublicEmotion_Enable=False),
            base_config(task_command='OpsiExplore'),
            base_config(PublicEmotion_Tasks=None),
        )
        for config in configs:
            for fleet in ('1', '2'):
                config.set_task_morale(fleet, 130)
            Emotion(config).reduce(fleet_index=2)
            self.assertEqual(128, config.Emotion_Fleet2Value)
            self.assertEqual(119, config.PublicEmotion_Fleet2Value)

    def test_emergency_reset_keeps_shared_ledger(self):
        """红脸保底只清任务自己的两个槽位。"""
        config = self.normal()
        for number in range(1, 7):
            config.set_morale(number, 100)
        Emotion(config).emergency_reset()
        self.assertEqual(0, config.Emotion_Fleet1Value)
        self.assertEqual(0, config.Emotion_Fleet2Value)
        self.assertEqual(100, config.PublicEmotion_Fleet1Value)

    # ---- 下面这些是共用模式已经逐条测过的场景，普通模式照着对齐一遍 ----

    def test_double_book_deducts_four_per_battle(self):
        """二倍经验书下单场扣 4 点，落点不变。"""
        config = self.normal()
        emotion = Emotion(config)
        emotion.map_is_2x_book = True
        emotion.reduce(fleet_index=1)
        emotion.reduce(fleet_index=1)
        self.assertEqual(122, config.Emotion_Fleet1Value)
        self.assertEqual(8, emotion.total_reduced)

    def test_shipwreck_deducts_ten(self):
        config = self.normal()
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1, shipwreck=True)
        self.assertEqual(120, config.Emotion_Fleet1Value)
        self.assertEqual(10, emotion.total_reduced)

    def test_shipwreck_charges_the_slot_even_if_it_stands_by(self):
        """普通模式按槽位记账：单队全清时 Boss 槽位没出击，沉船照样记在它头上。

        这是改造前的口径（共用模式才按真实舰队、按实际出击的那几支判定），
        列出来是为了钉住"普通模式没被顺手改掉"。
        """
        config = self.normal()
        config.set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        emotion = Emotion(config)
        emotion.reduce(fleet_index=2, shipwreck=True)
        self.assertEqual(130, config.Emotion_Fleet1Value)
        self.assertEqual(120, config.Emotion_Fleet2Value)

    def test_check_reduce_caps_expected_reduce_for_keep_exp_bonus(self):
        """「保持开心加成」时单次预计扣减封顶 29，否则会无限延迟。"""
        config = self.normal()
        config.set_task_morale('1', 10)
        config.Emotion_Fleet1Control = 'keep_exp_bonus'
        emotion = Emotion(config)
        recovered, _delay = emotion._check_reduce(16)     # 16 场 × 2 = 32 → 按 29 算
        expected = 120 + 29 - 10
        self.assertAlmostEqual(expected * 360 / 5, (recovered - current_time()).total_seconds(), delta=2)

    def test_keep_exp_bonus_with_harbor_asks_human(self):
        config = self.normal()
        config.set_task_morale('1', 10)
        config.Emotion_Fleet1Control = 'keep_exp_bonus'
        config.Emotion_Fleet1Recover = 'not_in_dormitory'
        with self.assertRaises(RequestHumanTakeover):
            Emotion(config)._check_reduce(1)

    def test_wait_targets_the_slot_fleet(self):
        """等待算的是槽位对应的舰队：1 队不足要等、2 队充足不用等。"""
        config = self.normal()
        config.set_task_morale('1', 10)
        config.set_task_morale('2', 119)
        emotion = Emotion(config)
        emotion.update()
        self.assertGreater(emotion.fleet_1.get_recovered(expected_reduce=2), current_time())
        self.assertLessEqual(emotion.fleet_2.get_recovered(expected_reduce=2), current_time())

    def test_recovery_caps_at_harbor_limit(self):
        config = self.normal()
        config.Emotion_Fleet1Recover = 'not_in_dormitory'
        config.set_task_morale('1', 100, record=current_time() - timedelta(days=10))
        emotion = Emotion(config)
        emotion.update()
        self.assertEqual(119, emotion.fleet_1.current)

    def test_future_record_does_not_subtract(self):
        config = self.normal()
        config.set_task_morale('1', 50, record=current_time() + timedelta(hours=1))
        emotion = Emotion(config)
        emotion.update()
        self.assertEqual(50, emotion.fleet_1.current)

    def test_negative_morale_is_read_back_as_zero(self):
        config = self.normal()
        config.set_task_morale('1', 0)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1)
        self.assertEqual(-2, config.Emotion_Fleet1Value)
        emotion.update()
        self.assertEqual(0, emotion.fleet_1.current)

    def test_total_reduced_triggers_bug_restart(self):
        """累计扣减超过阈值判定客户端算错心情——普通模式同样生效。"""
        config = self.normal()
        emotion = Emotion(config)
        for _ in range(60):        # 60 场 × 2 = 120，阈值随机落在 55~105，必定触发
            emotion.reduce(fleet_index=1)
        self.assertEqual(120, emotion.total_reduced)
        self.assertTrue(emotion.triggered_bug())
        self.assertEqual(0, emotion.total_reduced)

    def test_campaign_with_recovery_between_battles(self):
        """3 场战斗、每场间隔 6 分钟：扣 6 点、恢复 15 点（50 点/小时）。"""
        with Clock() as clock:
            config = base_config(PublicEmotion_Enable=False)
            config.set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_task_morale('1', 119, record=clock.now)
            emotion = Emotion(config)
            for _ in range(3):
                clock.advance(minutes=6)
                emotion.reduce(fleet_index=1)
            self.assertEqual(128, config.Emotion_Fleet1Value)

    def test_offline_recovery_after_restart(self):
        """关掉程序半小时再打开：从记录时间继续恢复。"""
        with Clock() as clock:
            config = base_config(PublicEmotion_Enable=False)
            config.set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_task_morale('1', 119, record=clock.now)
            first = Emotion(config)
            first.reduce(fleet_index=1)
            first.record()
            del first
            clock.advance(minutes=30)
            second = Emotion(config)
            second.update()
            self.assertEqual(142, second.fleet_1.current)

    def test_fractional_credit_survives_restart(self):
        """未满 1 点的余数记在记录时间里，反复重启也能攒够那 1 点。"""
        with Clock() as clock:
            config = base_config(PublicEmotion_Enable=False)
            config.set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_task_morale('1', 10, record=clock.now - timedelta(minutes=10))
            emotion = Emotion(config)
            emotion.update()
            emotion.record()
            self.assertEqual(18, config.Emotion_Fleet1Value)

            clock.advance(seconds=24)
            emotion = Emotion(config)
            emotion.update()
            emotion.record()
            self.assertEqual(18, config.Emotion_Fleet1Value)

            clock.advance(seconds=24)
            emotion = Emotion(config)
            emotion.update()
            emotion.record()
            self.assertEqual(19, config.Emotion_Fleet1Value)

    def test_switching_back_from_shared_continues_on_task_ledger(self):
        """关掉共用心情后，任务从自己槽位上的值继续记（值由记账时同步过去）。"""
        config = base_config(task_command='Main')          # 开始是共用模式
        config.set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 100, record=current_time())
        config.set_task_morale('1', 100)                   # 记账时同步到任务图的结果
        config.PublicEmotion_Enable = False                # 用户关掉共用心情

        emotion = Emotion(config)
        self.assertEqual({}, emotion.sharing)
        emotion.reduce(fleet_index=1)
        self.assertEqual(98, config.Emotion_Fleet1Value)
        self.assertEqual(100, config.PublicEmotion_Fleet1Value, '共用账本不再被这个任务动')


class TaskSlotMirrorTests(unittest.TestCase):
    """共用模式记账时，账本按舰队职能回写到任务自己的槽位。"""

    def test_record_mirrors_ledger_by_role(self):
        config = base_config().set_fleet(4, 3)
        config.set_morale(4, 119).set_morale(3, 119)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1)
        emotion.reduce(fleet_index=2)
        emotion.record()
        # 道中位（Fleet1*）跟着真实舰队4，Boss 位（Fleet2*）跟着真实舰队3。
        self.assertEqual(117, config.Emotion_Fleet1Value)
        self.assertEqual(117, config.Emotion_Fleet2Value)
        self.assertEqual(config.PublicEmotion_Fleet4Value, config.Emotion_Fleet1Value)
        self.assertEqual(config.PublicEmotion_Fleet3Value, config.Emotion_Fleet2Value)
        self.assertEqual(config.PublicEmotion_Fleet4Record, config.Emotion_Fleet1Record)

    def test_unused_slot_is_not_mirrored(self):
        """单队全清只同步道中位，Boss 位留给用户自己的设置。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 119)
        config.set_task_morale('2', 77)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1)
        emotion.record()
        self.assertEqual(117, config.Emotion_Fleet1Value)
        self.assertEqual(77, config.Emotion_Fleet2Value)

    def test_unmonitored_task_resumes_from_fresh_record(self):
        """被监控一阵再移出清单：回到普通记账时基准是新鲜的，不会按离线时间顶到上限。

        任务自己的记录时间停在"进清单之前"（这里构造为 30 天前），记账时不回写的话，
        重新普通记账的第一次 update() 会把这 30 天当成离线时间，直接算到上限 150。
        """
        with Clock() as clock:
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_morale(1, 100, record=clock.now)
            config.set_task_morale('1', 119, record=clock.now - timedelta(days=30))

            emotion = Emotion(config)
            emotion.reduce(fleet_index=1)
            emotion.record()
            self.assertEqual(98, config.Emotion_Fleet1Value, '账本按职能同步到任务槽位')
            self.assertEqual(
                int(config.PublicEmotion_Fleet1Record.timestamp()),
                int(config.Emotion_Fleet1Record.timestamp()),
                '记录时间一起同步，否则任务图会按旧时间戳把心情算高',
            )

            # 用户把它移出监控清单，任务改回普通记账：10 分钟恢复 8 点（后宅二楼 50/6min）。
            config._values['PublicEmotion_Tasks'] = 'Event'
            clock.advance(minutes=10)
            resumed = Emotion(config)
            self.assertEqual({}, resumed.sharing)
            resumed.update()
            self.assertEqual(106, resumed.fleet_1.current)


class LedgerTimeTests(unittest.TestCase):
    """共用账本的记录时间：手改只重置那一支舰队的基准，跨任务读同一个基准。"""

    def test_manual_edit_only_resets_that_fleet(self):
        """手改舰队4 的（值 + 时间）不影响舰队3 的基准。"""
        with Clock() as clock:
            config = base_config().set_fleet(4, 3)
            config.set_morale(4, 100, record=clock.now - timedelta(hours=1))
            config.set_morale(3, 100, record=clock.now - timedelta(hours=1))
            config.set_morale(4, 140, record=clock.now)      # 用户在面板上把舰队4 改成 140

            emotion = Emotion(config)
            emotion.update()
            self.assertEqual(140, emotion.sharing[4].current, '舰队4 从手改的那一刻起算')
            self.assertEqual(150, emotion.sharing[3].current, '舰队3 没被动过，照旧按 1 小时前算')

    def test_two_tasks_read_the_same_time_baseline(self):
        """共用同一支舰队的两个任务：一方记账，另一方接着按那个时间戳算恢复。"""
        with Clock() as clock:
            config = base_config(task_command='Main').set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_morale(1, 100, record=clock.now)
            first = Emotion(config)
            first.reduce(fleet_index=1)
            first.record()
            self.assertEqual(98, config.PublicEmotion_Fleet1Value)

            clock.advance(minutes=30)
            config.task.command = 'Event'
            second = Emotion(config)
            self.assertEqual({1: 1}, second.roles, 'Event 用的是同一支真实舰队')
            second.update()
            # 后宅二层 50 点/小时 → 30 分钟 25 点：98 + 25。
            self.assertEqual(123, second.sharing[1].current)


class LedgerFieldSchemaTests(unittest.TestCase):
    """账本字段必须能通过 API 写，任务图自己的记录时间仍保持只读。"""

    def test_shared_ledger_fields_are_writable(self):
        """记录时间标成 display: disabled 会让 API 直接拒写（READ_ONLY）。

        实测表现：面板上挂一条红色写入错误，值写进去了记录时间没写进去，后端随后按
        模板默认的 2020 年推算恢复量，把刚播种的心情一路顶到上限。共用账本的记录时间
        是播种/手改心情值的基准，必须可写；界面上的只读由前端保证。
        """
        args = read_file('module/config/argument/args.json')['General']['PublicEmotion']
        for number in range(1, 7):
            for suffix in ('Value', 'Record'):
                field = args[f'Fleet{number}{suffix}']
                with self.subTest(field=f'Fleet{number}{suffix}'):
                    self.assertNotIn(field.get('display'), ('hide', 'disabled', 'readonly'))
                    self.assertNotIn(field.get('type'), ('storage', 'stored', 'state', 'lock'))

    def test_api_accepts_ledger_record_write(self):
        """播种要写的这一组字段，走真实 ConfigService 能落盘（不报 READ_ONLY）。"""
        import tempfile

        from module.api.config_service import ConfigService
        from module.api.protocol import ConfigChange
        from tests.test_api import fixture

        with tempfile.TemporaryDirectory() as directory:
            service = ConfigService(fixture(directory))
            service.patch('testpilot', None, [
                ConfigChange(path='General.PublicEmotion.Fleet1Value', value=77),
                ConfigChange(path='General.PublicEmotion.Fleet1Record', value='2026-09-21 10:00:00'),
            ])
            ledger = service.get('testpilot')['values']['General']['PublicEmotion']
            self.assertEqual(77, ledger['Fleet1Value'])
            self.assertEqual('2026-09-21 10:00:00', ledger['Fleet1Record'])

    def test_task_emotion_record_stays_read_only(self):
        """任务图的心情记录时间由程序自己写（后端记账时按职能回写）。"""
        args = read_file('module/config/argument/args.json')
        for task in ('Main', 'Event'):
            with self.subTest(task=task):
                self.assertEqual('disabled', args[task]['Emotion']['Fleet1Record'].get('display'))


if __name__ == '__main__':
    unittest.main()
