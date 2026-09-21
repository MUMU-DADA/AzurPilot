"""共用心情的核心测算验证：延迟、等待、恢复、保底与 bug 检测。

与 test_public_emotion.py 的分工：
- test_public_emotion.py 覆盖"配置与映射"（真实舰队号翻译、跨任务共享、预检拆解）
- 本文件覆盖"这套心情到底能不能有效工作"：心情不足时会不会真的延迟/等待，
  时间恢复是否按恢复速度累积，以及红脸保底与客户端 bug 重启检测。

时间相关的断言一律用 `module.config.time_source.now()` 锚定。
它是 NTP 校准过的时间（带秒级 offset），与墙钟 `datetime.now()` 有偏差，
直接混用会让"整 2 小时应恢复 10 点"这类断言算错。
"""
import unittest
from datetime import datetime, timedelta
from time import sleep as time_sleep
from unittest import mock

from module.combat import emotion as emotion_module
from module.combat.emotion import Emotion
from module.config.time_source import now as current_time
from module.exception import RequestHumanTakeover, ScriptEnd
from tests.test_public_emotion import Clock, base_config


class DelayTests(unittest.TestCase):
    """心情不足时通过 task_delay + ScriptEnd 延迟任务。"""

    def test_check_reduce_delays_when_below_limit(self):
        # prevent_yellow_face 阈值 30，心情 10 且预计再扣 2，必须延迟。
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 10)
        config.task_delay = mock.Mock()
        with self.assertRaises(ScriptEnd):
            Emotion(config).check_reduce(1)
        config.task_delay.assert_called_once()

    def test_check_reduce_passes_when_enough(self):
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 119)
        config.task_delay = mock.Mock()
        Emotion(config).check_reduce(1)
        config.task_delay.assert_not_called()

    def test_check_reduce_skipped_when_mode_ignores(self):
        """Emotion_Mode 不含 calculate 时完全不算心情，再低也不延迟。"""
        config = base_config(Emotion_Mode='ignore').set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 0)
        config.task_delay = mock.Mock()
        Emotion(config).check_reduce(9)
        config.task_delay.assert_not_called()

    def test_keep_exp_bonus_caps_expected_reduce(self):
        """「保持开心加成」时单次预计扣减封顶 29，否则会无限延迟。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 10, record=current_time())
        config.PublicEmotion_Fleet1Control = 'keep_exp_bonus'
        emotion = Emotion(config)
        # 9 场 × 2 = 18 不该被砍；换成 32 点就必须按 29 算：120 + 29 - 10 = 139 点。
        recovered, _delay = emotion._check_reduce(16)
        self.assertAlmostEqual(139 * 360 / 5, (recovered - current_time()).total_seconds(), delta=2)

    def test_keep_exp_bonus_with_harbor_asks_human(self):
        """「保持开心加成」不能配「港区」：港区上限 119 撑不到 120，直接交人。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 10, record=current_time())
        config.PublicEmotion_Fleet1Control = 'keep_exp_bonus'
        config.PublicEmotion_Fleet1Recover = 'not_in_dormitory'
        emotion = Emotion(config)
        with self.assertRaises(RequestHumanTakeover):
            emotion._check_reduce(1)


class WaitTests(unittest.TestCase):
    """心情不足时 wait() 阻塞到恢复时间。

    wait() 的循环条件是 `current_time() > recovered`，而 `current_time()` 是
    NTP 校准时间：两次校准之间它是**冻结**的，不随真实流逝前进。因此替身里
    sleep 一小会儿没用——循环不会收敛（第一版测试就这样把测试挂死）。
    这里换成合成时钟：每次 sleep 直接把时间推 60 秒，循环次数与线上一致。
    """

    def run_wait(self, config, fleet_index):
        """用合成时钟跑一次 wait，返回 (sleep 次数, 每次睡的秒数)。"""
        clock = [current_time()]
        calls = []

        def fake_now():
            return clock[0]

        def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) > 1000:
                raise AssertionError('等待循环没有收敛，疑似 recovered 时间算错')
            clock[0] = clock[0] + timedelta(seconds=60)

        with mock.patch.object(emotion_module, 'current_time', fake_now), \
                mock.patch.object(emotion_module, 'sleep', fake_sleep):
            Emotion(config).wait(fleet_index=fleet_index)
        return calls

    def test_wait_blocks_until_recovered(self):
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        # 心情 10、阈值 30、速度 5 点/360 秒 → 恢复 20 点需 1440 秒 = 24 轮。
        config.set_morale(1, 10)
        calls = self.run_wait(config, 1)
        self.assertTrue(calls, '心情不足时应进入等待循环')
        waited = len(calls) * 60
        # 循环是「先 sleep 再比较当前时间」，因此会在所需时长之后的
        # 下一轮才退出；实测 24 轮到期、27 轮退出，这里给出确定区间。
        self.assertGreaterEqual(waited, 1440)
        self.assertLessEqual(waited, 1440 + 3 * 60)
        self.assertEqual({60}, set(calls), '等待步长应为 60 秒')

    def test_wait_returns_immediately_when_enough(self):
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 119)
        self.assertEqual([], self.run_wait(config, 1))

    def test_wait_skips_unused_role(self):
        """单队全清时 Boss 职能没出击，wait(2) 不该阻塞。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 0)
        self.assertEqual([], self.run_wait(config, 2))

    def test_wait_two_fleet_split_waits_for_that_real_fleet(self):
        """两队分工：wait(2) 等的是 Boss 那支真实舰队自己的恢复时间。

        Boss 舰队 10 点、阈值 30、战前按一次战斗 2 点算 → 需恢复 22 点
        ÷ 50 点/小时 = 1584 秒；道中队 119 点足够，不该被一起等。
        """
        config = base_config().set_fleet(4, 3)
        config.set_morale(4, 119, record=current_time())
        config.set_morale(3, 10, record=current_time())
        self.assertEqual([], self.run_wait(config, 1))

        calls = self.run_wait(config, 2)
        self.assertTrue(calls, 'Boss 舰队心情不足时应进入等待循环')
        self.assertGreaterEqual(len(calls) * 60, 1584)
        self.assertLessEqual(len(calls) * 60, 1584 + 3 * 60)


class RecoveryTests(unittest.TestCase):
    """按恢复速度与经过时间推算心情，余数靠回扣时间戳累积。

    速度单位是「每 360 秒 speed 点」：后宅二层 50/h 对应 speed=5，
    即 50 点/小时。
    """

    def test_update_recovers_by_elapsed_time(self):
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 10, record=current_time() - timedelta(hours=1))
        emotion = Emotion(config)
        emotion.update()
        # 1 小时 × 50 点/小时 = 50 点。
        self.assertEqual(60, emotion.sharing[1].current)

    def test_update_recovers_proportionally(self):
        """恢复量与经过时间成正比：半小时加一半。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 10, record=current_time() - timedelta(minutes=30))
        emotion = Emotion(config)
        emotion.update()
        self.assertEqual(35, emotion.sharing[1].current)

    def test_update_respects_maximum(self):
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 145, record=current_time() - timedelta(days=10))
        emotion = Emotion(config)
        emotion.update()
        # 后宅二层上限 150。
        self.assertEqual(150, emotion.sharing[1].current)

    def test_record_keeps_fractional_remainder(self):
        """未满 1 点的余数折算成秒数从记录时间里回扣，留给下次累积。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 10, record=current_time() - timedelta(minutes=10))
        emotion = Emotion(config)
        emotion.update()
        fleet = emotion.sharing[1]
        # 10 分钟 → 8.33 点，整数部分 8、余数 0.33。
        self.assertEqual(18, fleet.current)
        self.assertAlmostEqual(0.333, fleet._fractional_seconds, places=2)

        before = current_time()
        emotion.record()
        # 余数 0.33 点 ÷ 50 点/小时 = 24 秒，记录时间应回扣这 24 秒。
        self.assertLess(config.PublicEmotion_Fleet1Record, before)
        self.assertGreater(config.PublicEmotion_Fleet1Record, before - timedelta(seconds=25))

    def test_record_is_not_counted_twice(self):
        """写盘之后再 update() 不会把同一段恢复量重复加一遍。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 10, record=current_time() - timedelta(minutes=10))
        emotion = Emotion(config)
        emotion.update()
        emotion.record()
        settled = emotion.sharing[1].current
        emotion.update()
        self.assertEqual(settled, emotion.sharing[1].current)

    def test_speed_table_matches_recovery_place(self):
        """恢复地点决定速度：港区 20 点/小时、后宅一楼 40、二楼 50。"""
        for place, speed in (('not_in_dormitory', 2), ('dormitory_floor_1', 4), ('dormitory_floor_2', 5)):
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.PublicEmotion_Fleet1Recover = place
            self.assertEqual(speed, Emotion(config).sharing[1].speed, place)

    def test_offline_recovery_caps_at_place_limit(self):
        """港区上限 119（后宅两层是 150），离线再久也不会超过它。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.PublicEmotion_Fleet1Recover = 'not_in_dormitory'
        config.set_morale(1, 100, record=current_time() - timedelta(days=10))
        emotion = Emotion(config)
        emotion.update()
        self.assertEqual(119, emotion.sharing[1].current)

    def test_future_record_does_not_subtract(self):
        """记录时间落在未来（时钟偏差、手改过）时不倒扣，按 0 秒算。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 50, record=current_time() + timedelta(hours=1))
        emotion = Emotion(config)
        emotion.update()
        self.assertEqual(50, emotion.sharing[1].current)

    def test_default_record_saturates_to_limit(self):
        """记录时间还是模板默认（2020-01-01）时会一路顶到上限。

        这也是共用心情播种必须把 Value 与 Record 一起写的原因：只写 Value 的话
        （记录时间字段标了 display: disabled，API 会拒写），后端第一次 update()
        就会把刚播种的 130 顶成 150。任务图同理，因此镜像不碰这一字段，
        由后端记账时按职能回写。
        """
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 130, record=datetime(2020, 1, 1))
        emotion = Emotion(config)
        emotion.update()
        self.assertEqual(150, emotion.sharing[1].current)


class RecordChangeTests(unittest.TestCase):
    """记录时间被改写之后，恢复量按新基准算。

    记录时间的语义是"上次记账的那一刻"：在那之后的恢复量会在下一次 update() 补上。
    因此**改心情值时必须把记录时间一起改到当下**——播种与手改心情值都走这条路，
    只写值不改时间的话，同一段恢复量会被再算一遍（实测：播种 130 被顶成 150，
    就是记录时间还停在模板默认的 2020 年）。
    """

    def test_value_with_fresh_record_stays_put(self):
        """成对写（Value + Record=现在）之后 update() 不动这个值。"""
        with Clock() as clock:
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_morale(1, 130, record=clock.now)
            emotion = Emotion(config)
            emotion.update()
            self.assertEqual(130, emotion.sharing[1].current)

    def test_value_written_alone_recredits_recovery(self):
        """只写 Value、记录时间停在 10 分钟前：这 10 分钟的恢复量会被重复加一遍。"""
        with Clock() as clock:
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_morale(1, 130, record=clock.now - timedelta(minutes=10))
            emotion = Emotion(config)
            emotion.update()
            # 后宅二层 50 点/小时 → 10 分钟 8 点。
            self.assertEqual(138, emotion.sharing[1].current)

    def test_recovery_continues_from_the_new_baseline(self):
        """改写之后时间继续走：从改的那一刻起照常恢复，既不重复也不漏算。"""
        with Clock() as clock:
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_morale(1, 130, record=clock.now)
            clock.advance(minutes=6)
            emotion = Emotion(config)
            emotion.update()
            # 后宅二层 50 点/小时 → 6 分钟 5 点。
            self.assertEqual(135, emotion.sharing[1].current)

    def test_manual_edit_survives_restart(self):
        """手改把基准改到当下，改完关掉程序再打开：只按新基准算。"""
        with Clock() as clock:
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            # 账面是 1 小时前记下的 10，用户在 t 时刻手改成 130（值与时间一起写）。
            config.set_morale(1, 10, record=clock.now - timedelta(hours=1))
            config.set_morale(1, 130, record=clock.now)
            clock.advance(minutes=12)
            emotion = Emotion(config)
            emotion.update()
            # 只按改的那一刻算 12 分钟 → 10 点；若沿用改之前那 1 小时会是上限 150。
            self.assertEqual(140, emotion.sharing[1].current)

    def test_record_write_stamps_now(self):
        """记账写下的记录时间就是当下：余数为 0 时不回扣任何秒数。"""
        with Clock() as clock:
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_morale(1, 10, record=clock.now - timedelta(hours=1))
            emotion = Emotion(config)
            emotion.update()
            emotion.record()
            self.assertEqual(60, config.PublicEmotion_Fleet1Value)
            self.assertEqual(clock.now.replace(microsecond=0), config.PublicEmotion_Fleet1Record)

    def test_future_record_starts_counting_after_that_moment(self):
        """记录时间落在未来（时钟回拨或手改过）：先按 0 秒算，时间走过之后再补上。"""
        with Clock() as clock:
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_morale(1, 100, record=clock.now + timedelta(minutes=30))
            emotion = Emotion(config)
            emotion.update()
            self.assertEqual(100, emotion.sharing[1].current, '未来时间不倒扣')

            # 又过了 45 分钟，结算时刻已经落在 15 分钟前。
            clock.advance(minutes=45)
            emotion = Emotion(config)
            emotion.update()
            self.assertEqual(112, emotion.sharing[1].current)


class SafetyTests(unittest.TestCase):
    """红脸保底与客户端 bug 重启检测。"""

    def test_emergency_reset_zeroes_shared_fleets(self):
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 100)
        emotion = Emotion(config)
        emotion.emergency_reset()
        self.assertEqual(0, config.PublicEmotion_Fleet1Value)
        self.assertEqual(0, emotion.sharing[1].current)

    def test_emergency_reset_only_touches_own_fleets(self):
        """单队全清只重置用到的舰队，不波及其他任务共用的编号。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 100).set_morale(4, 100)
        Emotion(config).emergency_reset()
        self.assertEqual(0, config.PublicEmotion_Fleet1Value)
        self.assertEqual(100, config.PublicEmotion_Fleet4Value)

    def test_emergency_reset_makes_next_check_delay(self):
        """清零之后下一次预检必然延迟——保底的意义就在这里。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, 100, record=current_time())
        emotion = Emotion(config)
        emotion.emergency_reset()
        config.task_delay = mock.Mock()
        with self.assertRaises(ScriptEnd):
            emotion.check_reduce(6)
        config.task_delay.assert_called_once()

    def test_triggered_bug_after_enough_reduction(self):
        """累计扣减超过阈值时判定客户端算错心情，要求重启游戏。"""
        emotion = Emotion(base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby'))
        emotion.total_reduced = 1000  # 阈值随机落在 55~105，取足够大
        self.assertTrue(emotion.triggered_bug())
        self.assertEqual(0, emotion.total_reduced)

    def test_triggered_bug_false_below_threshold(self):
        emotion = Emotion(base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby'))
        emotion.total_reduced = 0
        self.assertFalse(emotion.triggered_bug())


class ShipwreckTests(unittest.TestCase):
    """沉船额外扣减与"无视沉船"开关。"""

    def test_shipwreck_reduces_more(self):
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby').set_morale(1, 119)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1, shipwreck=True)
        self.assertEqual(119 - emotion.reduce_shipwreck, config.PublicEmotion_Fleet1Value)

    def test_ignore_shipwreck_skips_penalty(self):
        # 共用模式下这一项按**该支真实舰队**的设置（舰队级开关见 FleetFlagTests）。
        config = base_config()
        config.set_fleet(1, 0, 'fleet1_all_fleet2_standby').set_morale(1, 119)
        config.set_fleet_flags(1, shipwreck=True)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1, shipwreck=True)
        self.assertEqual(119, config.PublicEmotion_Fleet1Value)
        self.assertEqual(0, emotion.total_reduced)

    def test_shipwreck_on_unused_role_is_noop(self):
        """沉船发生在没出击的职能上时无账可扣，也不计入累计量。"""
        config = base_config().set_fleet(5, 0, 'fleet1_all_fleet2_standby').set_morale(5, 119)
        emotion = Emotion(config)
        emotion.reduce(fleet_index=2, shipwreck=True)
        self.assertEqual(119, config.PublicEmotion_Fleet5Value)
        self.assertEqual(0, emotion.total_reduced)

    def test_negative_morale_is_read_back_as_zero(self):
        """扣到负数时存下来的可能是负值，但读取阶段会夹回 0，不会越陷越深。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby').set_morale(1, 0)
        config.set_morale(1, 0, record=current_time())
        emotion = Emotion(config)
        emotion.reduce(fleet_index=1)
        self.assertEqual(-2, config.PublicEmotion_Fleet1Value)
        emotion.update()
        self.assertEqual(0, emotion.sharing[1].current)


class MixedTests(unittest.TestCase):
    """消耗与增长交替：真实运行里两者总是混在一起。"""

    def single_fleet(self, clock, value):
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
        config.set_morale(1, value, record=clock.now)
        return config

    def test_campaign_with_recovery_between_battles(self):
        """3 场战斗、每场间隔 6 分钟：扣 6 点、恢复 15 点（50 点/小时）。"""
        with Clock() as clock:
            config = self.single_fleet(clock, 119)
            emotion = Emotion(config)
            for _ in range(3):
                clock.advance(minutes=6)
                emotion.reduce(fleet_index=1)
            self.assertEqual(119 - 6 + 15, config.PublicEmotion_Fleet1Value)

    def test_delay_then_recover_then_fight(self):
        """预检不足就延迟；等过恢复时间后同一本账立刻能开打。"""
        with Clock() as clock:
            config = self.single_fleet(clock, 20)
            emotion = Emotion(config)
            recovered, delay = emotion._check_reduce(6)
            self.assertTrue(delay, '20 点 + 阈值 30 + 预计 12 点，必须延迟')
            clock.now = recovered
            self.assertFalse(emotion._check_reduce(6)[1], '到了恢复时间就该放行')

    def test_shipwreck_and_recovery_mix(self):
        """普通战斗 + 沉船 + 时间流逝：三者的净效果。"""
        with Clock() as clock:
            config = self.single_fleet(clock, 50)
            emotion = Emotion(config)
            emotion.reduce(fleet_index=1)                     # 50 - 2 = 48
            clock.advance(minutes=6)                          # +5
            emotion.reduce(fleet_index=1, shipwreck=True)     # 53 - 10 = 43
            self.assertEqual(43, config.PublicEmotion_Fleet1Value)

    def test_emergency_reset_then_offline_recovery(self):
        """保底清零后不会被永久卡住：离线够久照样回到能打的状态。"""
        with Clock() as clock:
            config = self.single_fleet(clock, 100)
            emotion = Emotion(config)
            emotion.emergency_reset()
            self.assertTrue(emotion._check_reduce(1)[1], '清零后必然延迟')
            clock.advance(hours=7)                            # 7 小时 × 50 点
            self.assertFalse(emotion._check_reduce(1)[1])
            self.assertEqual(150, config.PublicEmotion_Fleet1Value)


class RestartTests(unittest.TestCase):
    """关闭再开启程序：只剩配置里的 Value + Record，恢复量得接着算。"""

    def test_offline_recovery_after_restart(self):
        """关掉程序半小时再打开：从记录时间继续恢复，不从头开始也不丢时间。"""
        with Clock() as clock:
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_morale(1, 119, record=clock.now)
            first = Emotion(config)
            first.reduce(fleet_index=1)      # 117
            first.record()
            del first                        # 关闭程序
            clock.advance(minutes=30)        # 关着的这半小时
            second = Emotion(config)         # 重新打开
            second.update()
            self.assertEqual(142, second.sharing[1].current, '117 + 半小时的 25 点')

    def test_fractional_credit_survives_restart(self):
        """未满 1 点的余数记在记录时间里，反复重启也能攒够那 1 点。"""
        with Clock() as clock:
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_morale(1, 10, record=clock.now - timedelta(minutes=10))
            emotion = Emotion(config)        # 每次循环都新建对象 = 重启一次
            emotion.update()
            emotion.record()
            self.assertEqual(18, config.PublicEmotion_Fleet1Value, '10 分钟 = 8.33 → 8')

            clock.advance(seconds=24)
            emotion = Emotion(config)
            emotion.update()
            emotion.record()
            self.assertEqual(18, config.PublicEmotion_Fleet1Value, '0.67 点还不够 1 点')

            clock.advance(seconds=24)
            emotion = Emotion(config)
            emotion.update()
            emotion.record()
            self.assertEqual(19, config.PublicEmotion_Fleet1Value, '余数攒够 1 点')

    def test_total_reduced_is_per_run(self):
        """累计扣减是进程内的（用来判断客户端心情 bug），重开程序归零。"""
        config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby').set_morale(1, 119)
        first = Emotion(config)
        for _ in range(3):
            first.reduce(fleet_index=1)
        self.assertEqual(6, first.total_reduced)
        self.assertEqual(0, Emotion(config).total_reduced)

    def test_restart_does_not_assume_full_morale(self):
        """重启后不会以为心情是满的：磁盘上的低心情照样触发延迟。"""
        with Clock() as clock:
            config = base_config().set_fleet(1, 0, 'fleet1_all_fleet2_standby')
            config.set_morale(1, 10, record=clock.now)
            config.task_delay = mock.Mock()
            with self.assertRaises(ScriptEnd):
                Emotion(config).check_reduce(6)
            config.task_delay.assert_called_once()


if __name__ == '__main__':
    unittest.main()
