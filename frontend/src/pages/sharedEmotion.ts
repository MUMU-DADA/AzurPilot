import type { Schema, Value, Values } from '../api/types'

/**
 * 真实舰队号：编队界面里的第几支舰队，取自任务设置里
 * 「出击舰队 - 一队/二队使用第 X 支舰队」（`Fleet.Fleet1` / `Fleet.Fleet2`）。
 *
 * 注意区分另外两个「舰队号」：屏幕上的出击位 1/2，以及
 * `fleet_current_index` 的 1 道中 / 2 Boss 逻辑编号，两者都不是真实舰队号。
 */
export type RealFleet = number

/** 真实舰队号 1~6，覆盖全部编队槽位。 */
export const REAL_FLEETS: RealFleet[] = [1, 2, 3, 4, 5, 6]

/** 真实舰队的心情字段后缀，与后端 `Emotion.FleetN*` 同构。 */
export const SLOT_FIELDS = ['Value', 'Record', 'Control', 'Recover', 'Oath', 'Onsen', 'IgnoreWarning', 'IgnoreShipwreck']

/** 任务设置里由共用心情接管的心情字段前缀。 */
export const EMOTION_FIELD_PREFIXES = ['Fleet1', 'Fleet2']

/** 一个任务用到哪些真实舰队以及各自的职能。 */
export interface FleetUse {
  task: string
  /** 真实舰队号 -> 逻辑编号（1 道中、2 Boss）。 */
  roles: Record<number, number>
}

/** 一支真实舰队与使用它的任务。 */
export interface FleetUsage {
  fleet: RealFleet
  tasks: string[]
  /** 该舰队在任务里承担的职能，1 道中 / 2 Boss。 */
  roles: number[]
}

function groupValues(values: Values, task: string, group: string): Record<string, Value> | undefined {
  const taskValues = values[task]
  if (!taskValues || typeof taskValues !== 'object') return undefined
  const fields = taskValues[group]
  if (!fields || typeof fields !== 'object') return undefined
  return fields as Record<string, Value>
}

/**
 * 按后端 `AzurLaneConfig.bind()` 的优先级取配置值。
 *
 * 同一个参数在多个分组里都有定义（活动任务同时有 `EventGeneral`、
 * `Event2` 和 `TaskBalancer`），后端按固定的 bind 顺序取第一个命中的，
 * 这里复刻同样的顺序，避免界面显示的舰队和实际出战的不一致。
 */
function boundValue(
  values: Values, schema: Schema | undefined, task: string, group: string, argument: string,
): Value | undefined {
  const candidates = ['EventGeneral', 'TaskBalancer', task, group]
  for (const candidate of candidates) {
    const live = groupValues(values, candidate, group)?.[argument]
    if (live !== undefined && live !== null) return live
    const fallback = schema?.args?.[candidate]?.[group]?.[argument]?.value
    if (fallback !== undefined && fallback !== null) return fallback
  }
  return undefined
}

/** 读出任务设置里的出击舰队与职能分工。 */
function fleetSetting(
  values: Values, schema: Schema | undefined, task: string,
): {fleet1: number; fleet2: number; order: string} {
  const read = (argument: string, fallback: number) => {
    const number = Number(boundValue(values, schema, task, 'Fleet', argument))
    return Number.isFinite(number) ? number : fallback
  }
  const order = boundValue(values, schema, task, 'Fleet', 'FleetOrder')
  return {
    fleet1: read('Fleet1', 1),
    fleet2: read('Fleet2', 0),
    order: typeof order === 'string' ? order : 'fleet1_mob_fleet2_boss',
  }
}

/**
 * 把任务配置翻译成真实舰队号与职能，与后端 `real_fleets_of()` 保持一致。
 *
 * 单队全清只编入道中位，Boss 位不编入；`Fleet2` 为 0 或两支编号相同时
 * 收敛为一支真实舰队，因此不会重复记一份账。
 */
export function fleetRolesOf(
  values: Values, schema: Schema | undefined, task: string,
): FleetUse {
  const {fleet1, fleet2, order} = fleetSetting(values, schema, task)
  const roles: Record<number, number> = {}
  if (order === 'fleet1_all_fleet2_standby') {
    if (fleet1) roles[fleet1] = 1
  } else if (order === 'fleet1_standby_fleet2_all') {
    if (fleet2) roles[fleet2] = 2
  } else if (fleet1 && fleet2 && fleet1 !== fleet2) {
    roles[fleet1] = 1
    roles[fleet2] = 2
  } else if (fleet1) {
    roles[fleet1] = 1
  } else if (fleet2) {
    roles[fleet2] = 2
  }
  return {task, roles}
}

/** 共用心情任务名单，按半角逗号拆分。 */
export function parseTasks(raw: unknown): string[] {
  if (typeof raw !== 'string') return []
  return raw.split(',').map(task => task.trim()).filter(Boolean)
}

/** 参与共用心情的任务名单，取自当前实例的配置。 */
export function parseParticipatingTasks(values: Values): string[] {
  return parseTasks(values?.General?.PublicEmotion?.Tasks)
}

export function sharedEmotionEnabled(values: Values): boolean {
  return values?.General?.PublicEmotion?.Enable === true
}

/**
 * 参与的每个任务用到哪些真实舰队，这是「真实舰队」面板的数据来源。
 *
 * 清单由参与任务推导而非固定几支：没被任何任务用到的舰队不会出现在界面上，
 * 因此不会给一支根本不出击的编队配心情。
 */
export function fleetUsageOf(
  values: Values, schema: Schema | undefined, tasks: string[],
): FleetUsage[] {
  const byFleet = new Map<RealFleet, FleetUse[]>()
  for (const task of tasks) {
    const use = fleetRolesOf(values, schema, task)
    for (const key of Object.keys(use.roles)) {
      const fleet = Number(key)
      if (!byFleet.has(fleet)) byFleet.set(fleet, [])
      byFleet.get(fleet)!.push(use)
    }
  }
  return [...byFleet.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([fleet, users]) => ({
      fleet,
      tasks: users.map(use => use.task),
      roles: [...new Set(users.map(use => use.roles[fleet]))].sort(),
    }))
}

/**
 * 任务设置里决定"哪几支舰队上场"的字段：被共用心情监听时锁掉。
 *
 * 改了它们等于换了记账对象：要重新播种、还要把镜像改写到别的职能槽位，
 * 于是任务设置的界面改动会一路牵动共用心情的界面与逻辑，成本会一直堆在这里。
 * 索性锁住——要改就先把这个任务从共用心情的监控清单里移除。
 */
const FLEET_SELECTION_FIELDS = new Set(['Fleet1', 'Fleet2', 'FleetOrder'])

/**
 * 下沉到面板里按真实舰队配置的任务级字段。
 *
 * 「心情设置」决定要不要无视红脸出击警告、「无视沉船心情惩罚」决定沉船扣不扣，
 * 两者在共用模式下都是"每支舰队一份"（面板里的 FleetNIgnoreWarning /
 * FleetNIgnoreShipwreck），任务级再去改就会各说各话。
 */
const HANDLED_BY_FLEET = new Set(['Mode', 'IgnoreShipwreck'])

/**
 * 任务设置里的字段是否已被共用心情接管。
 *
 * 命中时该字段只读并显示提示，避免两个界面改同一份设置：
 * - `Emotion.FleetN*` 的心情字段：启用共用心情后由共用账本统一管理；
 * - `Emotion.Mode` / `Emotion.IgnoreShipwreck`：这两项下沉到面板里**按真实舰队**
 *   配置（每支舰队的红脸警告与沉船惩罚可以不一样），任务级不再各自为政；
 * - `Fleet.Fleet1/Fleet2/FleetOrder` 的上场舰队与职能：本任务在监控清单里时锁掉。
 */
export function managedBySharedEmotion(
  values: Values, group: string, argument: string,
): boolean {
  if (!sharedEmotionEnabled(values)) return false
  if (group === 'Emotion') {
    if (HANDLED_BY_FLEET.has(argument)) return true
    return EMOTION_FIELD_PREFIXES.some(prefix => argument.startsWith(prefix))
  }
  if (group === 'Fleet') return FLEET_SELECTION_FIELDS.has(argument)
  return false
}

/**
 * 该任务的心情模式是否包含「计算心情消耗」。
 *
 * 不含时后端既不等待也不扣减（`Emotion.is_calculate` 为假），这个任务打掉的心情
 * 完全不进共用账本。普通模式下它只骗到自己，共用模式下账本偏高会同时骗到
 * 共用同一支舰队的其它任务，因此要在面板上标出来。
 *
 * 草稿优先：用户刚在任务页改过、还没落盘的值要立刻反映到标记上。
 * 读不到设置时按"会计算"处理，避免误报。
 */
export function taskCalculatesMorale(
  values: Values, schema: Schema | undefined, task: string,
  edits: PendingEdits = {},
): boolean {
  const path = `${task}.Emotion.Mode`
  const draft = edits[path]?.value
  const mode = draft !== undefined ? draft : boundValue(values, schema, task, 'Emotion', 'Mode')
  if (typeof mode !== 'string' || !mode) return true
  return mode.includes('calculate')
}

/** 任务图心情字段的写入计划：路径 -> 值。 */
export type EmotionWrites = Map<string, Value>

/**
 * 编辑队列里的字段草稿：判断取值要优先读它，而不是服务端快照。
 *
 * 条目里也包含刚提交成功、config 快照还没刷新的那些——它们的值就是要写下去的值，
 * 读它比读快照更接近用户此刻看到的东西。
 */
export interface PendingEdits {[path: string]: {value: Value}}

/** 写入计划器的状态：上次清单签名 + 上次下发的整批写入指纹。 */
export interface EmotionWriteState {
  signature: string
  emitted: string
}

/** 计划器的额外输入：本轮是不是"刚开启"，以及播种要写下的记录时间。 */
export interface EmotionPlanOptions {
  /** 刚开启共用心情：清单里涉及的舰队一律按当前最小值重新播种，等于从零开始监听。 */
  fresh?: boolean
  /** 播种写下的记录时间（取值到分钟，同一分钟内整批内容稳定，幂等门才拦得住）。 */
  now?: Value
}

/**
 * 共用心情的**唯一**写入计划器：一次算出"要写哪些字段、写什么值"。
 *
 * 播种与镜像原先各是一个 effect，在同一轮渲染里各自读到不同版本的输入
 * （播种已算出新值、config 草稿还是旧值），于是镜像会拿旧值把刚播种的值
 * 覆盖回去——实测写出 130 之后又写回 119，最后各任务图参差不齐。
 * 合并成一次计算后，两者共享同一份 `live` 值，顺序天然确定。
 *
 * 播种规则（三条路径各不相同）：
 * - **刚开启**（`fresh`）：清单里涉及的舰队全部按当前最小值重新播种，等于从零开始
 *   监听当前清单；没被用到的舰队清掉接管标记，免得以后加入任务时拿着上一轮的旧账本。
 * - **开启中新增任务**：只有"本纪元还没接管过"的舰队走播种，已有帐本原样保留。
 * - **任务改了出击舰队或职能**：新涉及的舰队按上面的规则补播种，职能变化只影响镜像
 *   写向哪个槽位；移除任务不重算（只是不再参考它，重算只会取更高的值，方向偏乐观）。
 *
 * "接管过没有"看账本的**记录时间**：还是模板默认值就说明本纪元没写过。
 * 播种时 Value 与 Record 必须一起写——只写 Value 的话，后端会拿模板默认的
 * 2020-01-01 推算恢复量，第一次 update() 就把心情顶到上限（实测 130 → 150）。
 *
 * 幂等门比对的是**整批写入**：连续两轮算出同一批就不重复下发（写入落盘会换掉
 * `values` 的引用、再求值一次，没有这道门会不停入队）。刻意不用"每个字段只发
 * 一次"的集合——共用值改走再改回原值时，那会让任务图停在中间值（面板 130、
 * 任务图 131），而整批比对允许"和上一批不同"的写入重新下发。
 */
export function planEmotionWrites(
  values: Values, schema: Schema | undefined, tasks: string[],
  state: EmotionWriteState, edits: PendingEdits = {}, options: EmotionPlanOptions = {},
): {writes: EmotionWrites; state: EmotionWriteState} {
  const {fresh = false, now} = options
  // 名单变了就不拿上一批写入做比对：换任务后要按新清单重新求值一遍。
  const signature = monitorSignature(tasks)
  const previous = state.signature === signature ? state.emitted : ''
  // 空清单照旧什么都不做；唯一例外是"刚开启"——那时没有涉及舰队可播种，
  // 但其余舰队的接管标记要清掉。
  if (!tasks.length && !fresh) return {writes: new Map(), state: {signature, emitted: ''}}

  const writes: EmotionWrites = new Map()

  // 先把"当前应有"的共用值算出来：草稿 + 本轮要播种的值。
  // 镜像必须读它而不是草稿——草稿还没同步，会拿旧值把刚播种的值覆盖回去。
  const minimums = minimumMoraleOf(values, schema, tasks)
  const fleets = Object.keys(minimums).map(Number)
  const {current, records, recordDefault} = ledgerMaps(values, schema, edits)
  const live: Record<RealFleet, number> = {}
  for (const fleet of fleets) {
    const value = current[fleet]
    if (value !== undefined) live[fleet] = value
  }

  for (const fleet of fleets) {
    const valuePath = `General.PublicEmotion.Fleet${fleet}Value`
    const recordPath = `General.PublicEmotion.Fleet${fleet}Record`
    // 这个字段有还没落盘的草稿：先让它落地。草稿一旦落盘会连记录时间一起写，
    // "已接管"标记自然成立，播种不会再回来覆盖；不拦住的话，播种会拿任务图上的旧值
    // 把用户刚填的数字顶掉（实测手填 66 被顶回 119），也会把自己的记录时间重复排队。
    if (edits[valuePath] !== undefined || edits[recordPath] !== undefined) continue
    if (!fresh && isSeeded(records[fleet], recordDefault)) continue
    const value = minimums[fleet]
    live[fleet] = value
    // 值没变就不必重写，避免和用户正在输入的草稿抢同一个字段。
    if (current[fleet] !== value) writes.set(valuePath, value)
    // 记录时间一起写：它既是接管标记，也是后端推算恢复量的基准。
    if (now !== undefined) writes.set(recordPath, now)
  }

  if (fresh && recordDefault !== undefined) {
    // 刚开启时把没参与本次清单的舰队标记清掉：以后加入任务时它们才会按新设置播种，
    // 而不是拿上一轮留下的旧账本把任务图的值顶回去。
    for (const fleet of REAL_FLEETS) {
      if (fleets.includes(fleet)) continue
      if (isSeeded(records[fleet], recordDefault)) {
        writes.set(`General.PublicEmotion.Fleet${fleet}Record`, recordDefault)
      }
    }
  }

  // 被监听的必须真的记账：心情模式选了「无视红脸出击警告」的、开着「无视沉船心情
  // 惩罚」的，都要掰回能记账的值。否则它打掉的心情/沉船扣减完全不进共用账本，
  // 账本偏高会同时骗到共用同一支舰队的其它任务。
  for (const task of tasks) {
    const modePath = `${task}.Emotion.Mode`
    if (edits[modePath] === undefined && !taskCalculatesMorale(values, schema, task, edits)) {
      writes.set(modePath, 'calculate')
    }
    const wreckPath = `${task}.Emotion.IgnoreShipwreck`
    if (edits[wreckPath] === undefined
        && boundValue(values, schema, task, 'Emotion', 'IgnoreShipwreck') === true) {
      writes.set(wreckPath, false)
    }
  }

  // 镜像与播种同属一次计划：固定用播种后的 live 求值，
  // 因此播种当场就反映到任务图，两者不会互相覆盖。
  for (const [path, value] of moraleMirrorOf(values, schema, tasks, live)) {
    writes.set(path, value)
  }

  const key = mirrorKey([...writes].map(([path, value]) => ({path, value})))
  if (!key || key === previous) return {writes: new Map(), state: {signature, emitted: previous}}
  return {writes, state: {signature, emitted: key}}
}

/**
 * 共用心情值 → 各监控任务图设置心情字段的镜像写入。
 *
 * 任务图设置里的 `Emotion.Fleet1Value/Fleet2Value` 在启用共用心情后是只读的，
 * 语义上应当跟随共用账本，否则同一支舰队在总览和任务图里会显示两个值。
 * 这里按各任务的职能把共用值映射回它自己的 `Emotion.FleetN*`：
 * 该任务的道中队用到的真实舰队，就写它的 `Fleet1*`；Boss 队写 `Fleet2*`。
 *
 * 只写心情值。任务图的 `Emotion.FleetNRecord` 标了 `display: disabled`（由程序
 * 自己维护），API 会拒写，硬写只会在面板上挂一条红色写入错误；记录时间改由后端
 * 记账时按职能回写（见 `Emotion.record()`），任务被移出监控清单后基准同样新鲜。
 *
 * Args:
 *     live: 本轮"活"的共用值（含刚播种的结果）。缺省时读 config 草稿。
 *
 * Returns:
 *     EmotionWrites: 需要写入的任务字段，已按路径去重。
 *         值相同的不返回，避免每次渲染都产生无谓写入。
 */
export function moraleMirrorOf(
  values: Values, schema: Schema | undefined, tasks: string[],
  live: Record<RealFleet, number> = {},
): EmotionWrites {
  const writes: EmotionWrites = new Map()
  for (const task of tasks) {
    const roles = fleetRolesOf(values, schema, task).roles
    // 按职能顺序（道中 → Boss）输出，读起来与任务图里的字段顺序一致。
    const ordered = Object.keys(roles).map(Number).sort((a, b) => roles[a] - roles[b])
    for (const fleet of ordered) {
      const argument = `Fleet${roles[fleet]}`
      const shared = live[fleet]
        ?? toMorale(boundValue(values, schema, 'General', 'PublicEmotion', `Fleet${fleet}Value`))
      const current = boundValue(values, schema, task, 'Emotion', `${argument}Value`)
      if (shared === undefined || Number(shared) === Number(current)) continue
      writes.set(`${task}.Emotion.${argument}Value`, shared)
    }
  }
  return writes
}

/** 把一批写入压成可比对的签名。 */
export function mirrorKey(writes: Array<{path: string; value: Value}>): string {
  return writes.map(item => `${item.path}=${JSON.stringify(item.value)}`).join('|')
}

/**
 * 账本的"本纪元接管过没有"标记：看**记录时间**。
 *
 * 还是模板默认的记录时间（2020-01-01）就说明这支舰队从没被播种过、后端也从没记过
 * 账。播种时 Value 与 Record 一起写，因此接管之后这个标记自然成立，不需要额外的
 * 状态字段；刷新页面、换浏览器打开都成立。
 *
 * 拿不到模板默认值时按"已接管"处理：宁可漏播一次，也不要在每次渲染时反复覆盖。
 */
export function isSeeded(record: Value | undefined, defaultRecord: Value | undefined): boolean {
  if (record === undefined || record === null || record === '') return false
  if (defaultRecord === undefined || defaultRecord === null || defaultRecord === '') return true
  return record !== defaultRecord
}

/** 播种写下的记录时间，取值到分钟。 */
export function seedRecordTime(date: Date = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + ` ${pad(date.getHours())}:${pad(date.getMinutes())}:00`
}

/**
 * 监控清单的签名：任务名单本身。
 *
 * 只记名单，不记各任务用到的舰队——被监听的上场舰队与职能已经在界面上锁住，
 * 改不了；计划是否要重新下发由**整批写入内容**决定（见 planEmotionWrites 的幂等门），
 * 因此这里不必跟着任务设置走，两边就不会互相牵动。
 */
export function monitorSignature(tasks: string[]): string {
  return tasks.join('|')
}

/**
 * 取各监控任务里对应真实舰队心情的最小值。
 *
 * 任务图设置里的 `Emotion.FleetN*` 按**职能**索引（1 道中、2 Boss），
 * 要先按该任务的 `Fleet.FleetOrder` 换算出它用到的真实舰队号与职能，
 * 才能取到「这支真实舰队」的心情。取最小值是保守做法：高估会让脚本以为
 * 还有心情、实际已经红脸，低估只是多等一会儿。
 *
 * Returns:
 *     Record<RealFleet, number>: 真实舰队号 -> 各任务中的最小心情值。
 *         某支舰队在所有任务里都读不到心情时不会出现在结果里。
 */
export function minimumMoraleOf(
  values: Values, schema: Schema | undefined, tasks: string[],
): Record<RealFleet, number> {
  const minimums: Record<RealFleet, number> = {}
  for (const task of tasks) {
    const roles = fleetRolesOf(values, schema, task).roles
    for (const key of Object.keys(roles)) {
      const fleet = Number(key)
      // 该任务自己记的心情同样按职能索引，因此用角色回查。
      const value = toMorale(boundValue(values, schema, task, 'Emotion', `Fleet${roles[fleet]}Value`))
      if (value === undefined) continue
      if (!(fleet in minimums) || value < minimums[fleet]) minimums[fleet] = value
    }
  }
  return minimums
}

/**
 * 读共用账本：各真实舰队当前的值、记录时间，以及模板默认的记录时间。
 *
 * 草稿优先：判断"这支舰队接管过没有"用的是**用户此刻看到的值**（配置 + 队列里
 * 还没落盘的输入），而不是服务端快照。否则用户在面板上刚改完、快照还没回来时，
 * 播种会把他这一笔当成"还没接管"再写一次，用旧值顶掉用户的输入
 * （实测：手填 66 被播种的 119 覆盖，面板弹回 119）。
 */
export function ledgerMaps(
  values: Values, schema: Schema | undefined,
  edits: PendingEdits = {},
): {
  current: Record<RealFleet, number>
  records: Record<RealFleet, Value>
  recordDefault: Value | undefined
} {
  const current: Record<RealFleet, number> = {}
  const records: Record<RealFleet, Value> = {}
  for (const fleet of REAL_FLEETS) {
    const argument = `Fleet${fleet}Value`
    const recordArgument = `Fleet${fleet}Record`
    const live = toMorale(edits[`General.PublicEmotion.${argument}`]?.value)
      ?? toMorale(values?.General?.PublicEmotion?.[argument])
    if (live !== undefined) current[fleet] = live
    const record = edits[`General.PublicEmotion.${recordArgument}`]?.value
      ?? values?.General?.PublicEmotion?.[recordArgument]
    if (record !== undefined) records[fleet] = record
  }
  // 六支舰队的记录时间共用一个模板默认值。
  const recordDefault = schema?.args?.General?.PublicEmotion?.Fleet1Record?.value
  return {current, records, recordDefault}
}

/**
 * 把配置值读成心情数字，读不出数字时返回 undefined。
 *
 * 不能用 `Number(raw)` 直接转：`Number('')` 是 0、`Number(null)` 也是 0，
 * 空字段会被当成"心情 0"参与取最小值，静默把共用心情写到最低。
 */
function toMorale(raw: Value | undefined): number | undefined {
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : undefined
  if (typeof raw !== 'string') return undefined
  const text = raw.trim()
  if (!text) return undefined
  const value = Number(text)
  return Number.isFinite(value) ? value : undefined
}
