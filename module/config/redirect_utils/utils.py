"""配置重定向工具函数集。

提供配置版本升级时的值转换函数。
当配置 schema 发生变更时（如选项名称修改、值格式调整），
这些函数负责将旧格式的配置值转换为新格式。

重定向函数在 ConfigUpdater.config_redirect() 中被调用，
确保用户升级后无需手动修改配置文件。

常见的重定向场景：
- 选项名称变更（如 'auto' → 'default'）
- 值格式调整（如布尔值 → 枚举值）
- 服务器名称规范化
"""

from module.config.deep import deep_get, deep_set
from module.config.server import to_server
from module.config.utils import filepath_args, read_file

# 参数默认值只在真正需要迁移时读一次：args.json 很大，而迁移是极少数情况。
_argument_defaults = None


def template_defaults():
    """读参数 schema 里的默认值，用于判断某个字段是否还被用户动过。"""
    global _argument_defaults
    if _argument_defaults is None:
        _argument_defaults = read_file(filepath_args())
    return _argument_defaults


def upload_redirect(value):
    """
    redirect attr about upload.
    """
    if isinstance(value, list):
        if not value[0] and not value[1]:
            return 'do_not'
        elif value[0] and not value[1]:
            return 'save'
        elif not value[0] and value[1]:
            return 'upload'
        else:
            return 'save_and_upload'
    else:
        if not value:
            return 'do_not'
        else:
            return 'save'


def api_redirect(value):
    """
    redirect attr about api.
    """
    if value == 'auto':
        return 'default'
    elif to_server(value) == 'cn':
        return 'cn_gz_reverse_proxy'
    else:
        return 'default'


def dossier_redirect(value):
    """
    OpsiDossierBeacon -> AttackMode
    """
    if value:
        return 'current_dossier'
    else:
        return 'current'


def enhance_favourite_redirect(value):
    """
    EnhanceFavourite -> ShipToEnhance
    """
    if value:
        return 'all'
    else:
        return 'favourite'


def enhance_check_redirect(value):
    """
    CheckPerCategory should be at least 5
    """
    if isinstance(value, int):
        if value < 5:
            return 5
    return value


def emotion_mode_redirect(value):
    """
    CalculateEmotion + IgnoreLowEmotionWarn -> Emotion.Mode
    """
    calculate, ignore = value
    if calculate:
        if ignore:
            return 'calculate_ignore'
        else:
            return 'calculate'
    else:
        if ignore:
            return 'ignore'
        else:
            # Invalid, fallback to calculate
            return 'calculate'


def change_ship_redirect(value):
    """
    FlagshipChange + FlagshipEquipChange -> ChangeFlagship
    """
    ship, equip = value
    if not ship:
        return 'disabled'
    elif equip:
        return 'ship_equip'
    else:
        return 'ship'


def api_redirect2(value):
    """
    remove shanghai proxy, use guangzhou
    """
    if value == 'cn_sh_reverse_proxy':
        return 'cn_gz_reverse_proxy'
    else:
        return value


def coalition_to_frostfall(value):
    """
    将通用难度名转换为霜落活动的内部关卡编号。
    """
    if value == 'easy':
        return 'tc1'
    elif value == 'normal':
        return 'tc2'
    elif value == 'hard':
        return 'tc3'
    else:
        return value


def coalition_to_little_academy(value):
    """
    将旧联动活动的 TC 关卡编号转换为通用难度名。
    """
    normalized = str(value).lower().replace('-', '')
    if normalized == 'tc1':
        return 'easy'
    elif normalized == 'tc2':
        return 'normal'
    elif normalized == 'tc3':
        return 'hard'
    else:
        return value


def execute_fixed_patrol_scan_redirect(value):
    """
    OpsiHazard1Leveling.ExecuteFixedPatrolScan 旧等级枚举 → 布尔开关。

    该配置的形态变过两轮：布尔开关 → 0/1/2 等级 → 又合并回开关（保守模式
    并入效率模式）。存量配置里的数字档位要清洗成布尔，否则复选框会显示
    数字，运行时也容易读错：
    - 0（关闭）           → False
    - 1/2/3（各档强制移动）→ True（等级已合并成同一个效率模式）
    - 布尔值直接透传。
    """
    if isinstance(value, bool):
        return value
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return bool(value)


def public_emotion_to_real_fleets_redirect(new, old):
    """把共用心情的旧单槽位配置迁移到按真实舰队拆分的字段。

    拆分前 `General.PublicEmotion.Fleet*` 只表示一支舰队（当时的"共享池"）。
    迁移后每支**真实舰队**各有独立的 Value/Record/Control/Recover/Oath/Onsen，
    `FleetN` 里的 N 是编队界面里的第几支舰队。

    只有当**用户配置文件里真的存在**旧字段、**且目标字段还停在模板默认值**时才迁移：

    - 不能用 `new` 判断旧字段是否存在：`new` 是合并模板默认值之后的字典，新字段总是
      存在（拿到的是模板默认值），否则会把默认值当成用户设置；
    - 也不能只看 `old` 里有没有旧字段：配置是按"逐条合并修改"写回文件的，schema 里
      删掉的老字段会一直留在用户文件里，于是迁移每次加载都会重跑，把脚本已经记上账的
      新值覆盖回旧值（实测把 `Fleet1Value` 从 100 顶回 0、记录时间倒回两天前）；
    - 也不能只看 `old` 里有没有新字段：保存配置会把整个 schema 落盘，新字段可能只是
      被写了一份模板默认值，那时该迁移却会被跳过。

    旧配置表达不出它对应的是哪一支真实舰队，这里统按舰队 1 迁移；
    这是尽力而为的映射，用户核对后可在界面上改到正确的舰队。
    其余舰队保持默认，不会平白多出几份心情。

    Args:
        new (dict): 合并默认值后的新配置，会被就地修改。
        old (dict): 用户配置文件读出的原始配置。

    Returns:
        dict: 迁移后的新配置。
    """
    base = 'General.PublicEmotion'
    if deep_get(old, f'{base}.FleetValue') is None:
        # 用户从未用过共用心情，新字段保持默认。
        return new
    defaults = template_defaults()
    for suffix in ('Value', 'Record', 'Control', 'Recover', 'Oath', 'Onsen'):
        value = deep_get(old, f'{base}.Fleet{suffix}')
        target = f'{base}.Fleet1{suffix}'
        if value is None or deep_get(new, target) != deep_get(defaults, f'{target}.value'):
            # 目标字段已经不是默认值：说明它被记账或手改过，不再拿旧值覆盖。
            continue
        deep_set(new, keys=target, value=value)
    return new


# 声明签名是 (new, old)，由 ConfigUpdater.config_redirect 识别并单独派发。
public_emotion_to_real_fleets_redirect.takes_config = True
