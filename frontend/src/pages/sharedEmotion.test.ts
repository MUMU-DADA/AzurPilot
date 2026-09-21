import { describe, expect, it } from 'vitest'
import type { Schema, Values } from '../api/types'
import {
  SLOT_FIELDS,
  fleetRoleSetsOf,
  fleetRolesOf,
  fleetUsageOf,
  isSeeded,
  managedBySharedEmotion,
  minimumMoraleOf,
  monitorSignature,
  moraleMirrorOf,
  parseParticipatingTasks,
  planEmotionWrites,
  seedRecordTime,
  sharedEmotionEnabled,
  taskCalculatesMorale,
} from './sharedEmotion'

function values(publicEmotion: Record<string, unknown> = {}, tasks: Record<string, Record<string, unknown>> = {}): Values {
  return {
    General: {PublicEmotion: publicEmotion as never},
    ...tasks,
  } as unknown as Values
}

function schema(entries: Record<string, Record<string, unknown>>): Schema {
  return {menu: {}, args: entries as never, translations: {}}
}

/** 构造一个任务的出击舰队设置。 */
function fleetTask(fleet1: number, fleet2: number, order: string) {
  return {Fleet: {Fleet1: fleet1, Fleet2: fleet2, FleetOrder: order}}
}

describe('fleetRolesOf', () => {
  it('两队分工用两支真实舰队', () => {
    const use = fleetRolesOf(values({}, {Main: fleetTask(4, 3, 'fleet1_mob_fleet2_boss')}), undefined, 'Main')
    expect(use.roles).toEqual({4: 1, 3: 2})
  })

  it('职能互换不改变真实舰队号', () => {
    const use = fleetRolesOf(values({}, {Main: fleetTask(3, 4, 'fleet1_boss_fleet2_mob')}), undefined, 'Main')
    expect(use.roles).toEqual({3: 1, 4: 2})
  })

  it('单队全清只占道中位', () => {
    const use = fleetRolesOf(values({}, {Main: fleetTask(5, 0, 'fleet1_all_fleet2_standby')}), undefined, 'Main')
    expect(use.roles).toEqual({5: 1})
  })

  it('单队全清在第二槽位时占 Boss 位', () => {
    const use = fleetRolesOf(values({}, {Main: fleetTask(1, 6, 'fleet1_standby_fleet2_all')}), undefined, 'Main')
    expect(use.roles).toEqual({6: 2})
  })

  it('二队为 0 表示只带一支，职能声明的 Boss 位不成立', () => {
    const use = fleetRolesOf(values({}, {Main: fleetTask(1, 0, 'fleet1_mob_fleet2_boss')}), undefined, 'Main')
    expect(use.roles).toEqual({1: 1})
  })

  it('同一编号占两个出击位时只记一份账', () => {
    const use = fleetRolesOf(values({}, {Main: fleetTask(5, 5, 'fleet1_mob_fleet2_boss')}), undefined, 'Main')
    expect(use.roles).toEqual({5: 1})
  })

  it('同一编号双队分工保留两个职能，供共用账本取值与镜像', () => {
    expect(fleetRoleSetsOf(values({}, {Main: fleetTask(5, 5, 'fleet1_mob_fleet2_boss')}), undefined, 'Main'))
      .toEqual({5: [1, 2]})
  })

  it('配置里没有时回落到 schema 默认值', () => {
    const use = fleetRolesOf(values({}, {Main: {}}), schema({
      Main: {Fleet: {
        Fleet1: {type: 'select', value: 6},
        Fleet2: {type: 'select', value: 0},
        FleetOrder: {type: 'select', value: 'fleet1_all_fleet2_standby'},
      }},
    }), 'Main')
    expect(use.roles).toEqual({6: 1})
  })

  it('活动任务按后端 bind 顺序取 EventGeneral 的设置', () => {
    // 后端 bind 顺序是 EventGeneral 优先于 Event2 与任务自身，
    // 界面必须跟着取同一份，否则显示的舰队会和实际出战的不一致。
    const use = fleetRolesOf(values({}, {
      Event: fleetTask(1, 2, 'fleet1_mob_fleet2_boss'),
      EventGeneral: fleetTask(5, 0, 'fleet1_all_fleet2_standby'),
    }), undefined, 'Event')
    expect(use.roles).toEqual({5: 1})
  })

  it('读不到任何设置时回落到 schema 的 Fleet1 默认值', () => {
    // Fleet1 的 schema 默认值是 1，因此仍能确定一支舰队。
    expect(fleetRolesOf(values({}, {Main: {}}), undefined, 'Main').roles).toEqual({1: 1})
  })

  it('两支舰队都是 0 时不产生舰队', () => {
    const use = fleetRolesOf(values({}, {Main: fleetTask(0, 0, 'fleet1_mob_fleet2_boss')}), undefined, 'Main')
    expect(use.roles).toEqual({})
  })

  it('未知任务不会按默认值凭空占用 Fleet1', () => {
    expect(fleetRolesOf(values({}, {}), undefined, 'TypoTask').roles).toEqual({})
  })
})

describe('fleetUsageOf', () => {
  it('反转顺序只改变展示职能，不改变心情账本槽位', () => {
    const config = values({}, {Main: fleetTask(4, 3, 'fleet1_boss_fleet2_mob')})
    expect(fleetUsageOf(config, undefined, ['Main'])).toEqual([
      {fleet: 3, tasks: ['Main'], roles: [1]},
      {fleet: 4, tasks: ['Main'], roles: [2]},
    ])
    expect(fleetRoleSetsOf(config, undefined, 'Main')).toEqual({4: [1], 3: [2]})
  })

  it('按真实舰队号聚合，并记录职能', () => {
    const config = values({}, {Main: fleetTask(4, 3, 'fleet1_mob_fleet2_boss')})
    expect(fleetUsageOf(config, undefined, ['Main'])).toEqual([
      {fleet: 3, tasks: ['Main'], roles: [2]},
      {fleet: 4, tasks: ['Main'], roles: [1]},
    ])
  })

  it('面板字段与通用渲染互补，同一支舰队不会出现两份', () => {
    // TaskConfig 会把面板渲染的 FleetN* 从通用渲染里排除，
    // 因此面板给出的舰队必须与它的字段一一对应。
    const config = values({}, {Main: fleetTask(1, 0, 'fleet1_all_fleet2_standby')})
    const usage = fleetUsageOf(config, undefined, ['Main'])
    const panelFields = usage.flatMap(({fleet}) => SLOT_FIELDS.map(suffix => `Fleet${fleet}${suffix}`))
    expect(usage.map(item => item.fleet)).toEqual([1])
    expect(panelFields).toEqual([
      'Fleet1Value', 'Fleet1Record', 'Fleet1Control',
      'Fleet1Recover', 'Fleet1Oath', 'Fleet1Onsen',
      'Fleet1IgnoreWarning', 'Fleet1IgnoreShipwreck',
    ])
    // 未使用的舰队不在面板里，仍由通用渲染负责。
    expect(panelFields.some(field => field.startsWith('Fleet2'))).toBe(false)
  })

  it('没被任何任务用到的舰队不出现', () => {
    const config = values({}, {
      Main: fleetTask(5, 0, 'fleet1_all_fleet2_standby'),
      Event: fleetTask(5, 0, 'fleet1_all_fleet2_standby'),
    })
    expect(fleetUsageOf(config, undefined, ['Main', 'Event'])).toEqual([
      {fleet: 5, tasks: ['Main', 'Event'], roles: [1]},
    ])
  })

  it('不同任务用不同舰队时各列一项', () => {
    const config = values({}, {
      Main: fleetTask(4, 0, 'fleet1_all_fleet2_standby'),
      Event: fleetTask(3, 0, 'fleet1_all_fleet2_standby'),
    })
    expect(fleetUsageOf(config, undefined, ['Main', 'Event'])).toEqual([
      {fleet: 3, tasks: ['Event'], roles: [1]},
      {fleet: 4, tasks: ['Main'], roles: [1]},
    ])
  })

  it('共有舰队合并职能并去重', () => {
    const config = values({}, {
      Main: fleetTask(4, 3, 'fleet1_mob_fleet2_boss'),
      Event: fleetTask(3, 4, 'fleet1_mob_fleet2_boss'),
    })
    expect(fleetUsageOf(config, undefined, ['Main', 'Event'])).toEqual([
      {fleet: 3, tasks: ['Main', 'Event'], roles: [1, 2]},
      {fleet: 4, tasks: ['Main', 'Event'], roles: [1, 2]},
    ])
  })
})

describe('parseParticipatingTasks', () => {
  it('按半角逗号拆分并去掉空白', () => {
    expect(parseParticipatingTasks(values({Tasks: 'Main2, Event , EventA'})))
      .toEqual(['Main2', 'Event', 'EventA'])
  })

  it('重复任务只保留一份，避免面板与签名重复', () => {
    expect(parseParticipatingTasks(values({Tasks: 'Main, Main, Event, Main'})))
      .toEqual(['Main', 'Event'])
  })

  it('空值得到空名单', () => {
    expect(parseParticipatingTasks(values({Tasks: null}))).toEqual([])
    expect(parseParticipatingTasks(values())).toEqual([])
  })
})

describe('sharedEmotionEnabled', () => {
  it('只有显式 true 才算启用', () => {
    expect(sharedEmotionEnabled(values({Enable: true}))).toBe(true)
    expect(sharedEmotionEnabled(values({Enable: false}))).toBe(false)
    expect(sharedEmotionEnabled(values({Enable: 'true'}))).toBe(false)
    expect(sharedEmotionEnabled(values())).toBe(false)
  })
})

describe('managedBySharedEmotion', () => {
  it('启用共用心情后接管 Emotion 组的舰队心情字段', () => {
    const config = values({Enable: true})
    expect(managedBySharedEmotion(config, 'Emotion', 'Fleet1Value')).toBe(true)
    expect(managedBySharedEmotion(config, 'Emotion', 'Fleet2Recover')).toBe(true)
  })

  it('接管被下沉到面板的两项任务级设置', () => {
    // 「心情设置」里的"无视红脸出击警告"与「无视沉船心情惩罚」现在都按真实舰队配置，
    // 任务级整项交给面板，改了就会各说各话。
    const config = values({Enable: true})
    expect(managedBySharedEmotion(config, 'Emotion', 'Mode')).toBe(true)
    expect(managedBySharedEmotion(config, 'Emotion', 'IgnoreShipwreck')).toBe(true)
  })

  it('接管出击舰队的 Fleet1/Fleet2 选择与职能', () => {
    // 改了上场舰队或职能等于换了记账对象：要重新播种、镜像也要改槽位，
    // 于是任务设置的改动会一路牵动共用心情，索性在被监听期间锁住。
    const config = values({Enable: true})
    expect(managedBySharedEmotion(config, 'Fleet', 'Fleet1')).toBe(true)
    expect(managedBySharedEmotion(config, 'Fleet', 'Fleet2')).toBe(true)
    expect(managedBySharedEmotion(config, 'Fleet', 'FleetOrder')).toBe(true)
  })

  it('不接管出击舰队的其他设置', () => {
    // 阵型、战斗模式、步数不影响"哪几支舰队上场"，照旧可改。
    const config = values({Enable: true})
    for (const argument of ['Fleet1Formation', 'Fleet1Mode', 'Fleet1Step', 'SkipPreparation']) {
      expect(managedBySharedEmotion(config, 'Fleet', argument)).toBe(false)
    }
  })

  it('接管「无视沉船心情惩罚」', () => {
    // 忽略沉船扣减会让共用账本少记 10 点，和心情字段一样由账本统一管理。
    const config = values({Enable: true})
    expect(managedBySharedEmotion(config, 'Emotion', 'IgnoreShipwreck')).toBe(true)
  })

  it('关闭开关时什么都不接管', () => {
    const config = values({Enable: false})
    expect(managedBySharedEmotion(config, 'Emotion', 'IgnoreShipwreck')).toBe(false)
    expect(managedBySharedEmotion(config, 'Fleet', 'Fleet1')).toBe(false)
    expect(managedBySharedEmotion(config, 'Emotion', 'Fleet1Value')).toBe(false)
  })
})

describe('minimumMoraleOf', () => {
  /** 带心情值的任务设置。 */
  const moraleTask = (fleet1: number, fleet2: number, order: string,
                      fleet1Value: number, fleet2Value = 119) => ({
    Fleet: {Fleet1: fleet1, Fleet2: fleet2, FleetOrder: order},
    Emotion: {Fleet1Value: fleet1Value, Fleet2Value: fleet2Value},
  })

  it('同一支真实舰队被多个任务用，取其中的最小值', () => {
    // 三个任务都用真实舰队1 打道中，心情分别 150 / 130 / 130。
    const config = values({}, {
      Main2: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 150),
      Event: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 130),
      EventA: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 130),
    })
    expect(minimumMoraleOf(config, undefined, ['Main2', 'Event', 'EventA'])).toEqual({1: 130})
  })

  it('同一真实舰队占两个任务槽位时，两个槽位的较低心情生效', () => {
    const config = values({}, {
      Main: moraleTask(5, 5, 'fleet1_mob_fleet2_boss', 100, 40),
    })
    expect(minimumMoraleOf(config, undefined, ['Main'])).toEqual({5: 40})
  })

  it('按职能换算：二队打道中时读的是该任务的 Fleet2Value', () => {
    // 职能是「一队boss二队道中」，道中队是真实舰队3，对应 Emotion.Fleet2Value。
    const config = values({}, {
      Main: moraleTask(4, 3, 'fleet1_boss_fleet2_mob', 119, 42),
    })
    expect(minimumMoraleOf(config, undefined, ['Main'])).toEqual({4: 119, 3: 42})
  })

  it('不同任务用不同舰队时各取各的', () => {
    const config = values({}, {
      Main: moraleTask(4, 0, 'fleet1_all_fleet2_standby', 100),
      Event: moraleTask(3, 0, 'fleet1_all_fleet2_standby', 60),
    })
    expect(minimumMoraleOf(config, undefined, ['Main', 'Event'])).toEqual({4: 100, 3: 60})
  })

  it('读不到心情的任务被跳过', () => {
    const config = values({}, {
      Main: {Fleet: {Fleet1: 4, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'}},
    })
    expect(minimumMoraleOf(config, undefined, ['Main'])).toEqual({})
  })

  it('空名单得到空结果', () => {
    expect(minimumMoraleOf(values({}), undefined, [])).toEqual({})
  })
})

describe('初始接管：监控清单第一次设置时的播种', () => {
  /** 带心情值的任务设置。 */
  const moraleTask = (fleet1: number, fleet2: number, order: string,
                      fleet1Value: number, fleet2Value = 119) => ({
    Fleet: {Fleet1: fleet1, Fleet2: fleet2, FleetOrder: order},
    Emotion: {Fleet1Value: fleet1Value, Fleet2Value: fleet2Value},
  })

  /**
   * 模拟面板的一串操作：每次改清单都求一次统一的写入计划，并把写出去的内容
   * 反馈进"草稿"（等价于 config 同步回来的结果），状态在多次操作间保持。
   * 返回这次真正下发出去的写入（形如 `路径=值`）。
   */
  const session = (tasks: Record<string, ReturnType<typeof moraleTask>>, shared: Record<number, number> = {}) => {
    const draft = values({}, {...tasks})
    let state = {signature: '', emitted: ''}
    const sync = (list: string[]) => {
      const general = (draft.General.PublicEmotion ?? {}) as Record<string, unknown>
      draft.General.PublicEmotion = {Tasks: list.join(', '), ...general} as never
      for (const [fleet, value] of Object.entries(shared)) general[`Fleet${fleet}Value`] = value
    }
    return (list: string[]) => {
      sync(list)
      const {writes, state: next} = planEmotionWrites(draft, undefined, list, state)
      state = next
      for (const [path, value] of writes) {
        const [owner, group, argument] = path.split('.')
        ;(draft[owner][group] as Record<string, unknown>)[argument] = value
        if (owner === 'General' && argument === `Fleet${argument.match(/\d+/)?.[0]}Value`) {
          shared[Number(argument.match(/\d+/)?.[0])] = Number(value)
        }
      }
      return [...writes].map(([path, value]) => `${path}=${JSON.stringify(value)}`)
    }
  }

  it('只有一个监控任务时，直接采用该任务的心情', () => {
    const change = session({Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 88)})
    expect(change(['Main'])).toContain('General.PublicEmotion.Fleet1Value=88')
  })

  it('多个监控任务共用同一支舰队时取最小值', () => {
    const change = session({
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 150),
      Main2: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 130),
      Event: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 140),
    })
    expect(change(['Main', 'Main2', 'Event'])).toContain('General.PublicEmotion.Fleet1Value=130')
  })

  it('两队分工的任务把两支舰队分别播种', () => {
    const change = session({Main: moraleTask(4, 3, 'fleet1_mob_fleet2_boss', 120, 90)})
    const writes = change(['Main'])
    expect(writes).toContain('General.PublicEmotion.Fleet3Value=90')
    expect(writes).toContain('General.PublicEmotion.Fleet4Value=120')
  })

  it('单队全清的任务只播种它用到的道中位', () => {
    // 二队配了舰队3 但职能是待机，因此舰队3 不该被播种。
    const change = session({Main: moraleTask(1, 3, 'fleet1_all_fleet2_standby', 100, 55)})
    const writes = change(['Main'])
    expect(writes).toContain('General.PublicEmotion.Fleet1Value=100')
    expect(writes.some(item => item.startsWith('General.PublicEmotion.Fleet3Value'))).toBe(false)
  })

  it('一队待机二队全清时播种 Boss 位对应的舰队', () => {
    const change = session({Main: moraleTask(1, 6, 'fleet1_standby_fleet2_all', 100, 66)})
    expect(change(['Main'])).toContain('General.PublicEmotion.Fleet6Value=66')
  })

  it('没在监控名单里的任务不参与播种', () => {
    // Main2 心情只有 20，但它没被监控，不该把共用账本拉低。
    const change = session({
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 110),
      Main2: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 20),
    })
    expect(change(['Main'])).toContain('General.PublicEmotion.Fleet1Value=110')
  })

  it('任务图读不到任何心情时不播种，保留面板现有值', () => {
    const change = session({
      Main: {
        Fleet: {Fleet1: 4, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'},
        Emotion: {Fleet1Value: '' as unknown as number, Fleet2Value: 119},
      },
    })
    expect(change(['Main']).some(item => item.startsWith('General.PublicEmotion.Fleet4Value'))).toBe(false)
  })

  it('清单没变时不重复下发（避免每次渲染都覆盖手改的值）', () => {
    const change = session({Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 88)})
    expect(change(['Main'])).not.toHaveLength(0)
    expect(change(['Main'])).toEqual([])
  })

  it('播种的同时把任务图镜像到新值', () => {
    // 这两件事原本是两个 effect，会在同一轮渲染里互相覆盖；现在同属一次计划。
    // 两个任务共用舰队1，取最小值 100 播种，并把还停在 130 的任务图改过来。
    const change = session({
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 100),
      Main2: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 130),
    })
    const writes = change(['Main', 'Main2'])
    expect(writes).toContain('General.PublicEmotion.Fleet1Value=100')
    // Main 已经是 100，只有停在 130 的 Main2 需要镜像。
    expect(writes).toContain('Main2.Emotion.Fleet1Value=100')
    expect(writes.some(item => item.startsWith('Main.Emotion.Fleet1Value'))).toBe(false)
  })

  it('用户刚填进面板、还没落盘的值不会被播种顶掉', () => {
    // 快照里还是模板默认值 119，但队列里已经有用户填的 66：播种若按快照判定
    // "还没接管"，就会把 66 顶成 119——实测面板手改后弹回原值就是这么来的。
    const config = values({}, {
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 100),
    })
    config.General.PublicEmotion = {Tasks: 'Main', Fleet1Value: 119} as never
    const args = schema({General: {PublicEmotion: {Fleet1Value: {value: 119}}}})
    const edits = {'General.PublicEmotion.Fleet1Value': {value: '66'}}
    const {writes} = planEmotionWrites(config, args, ['Main'], {signature: '', emitted: ''}, edits)
    // 只把用户的 66 镜像下去，不再按快照播种 100。
    expect([...writes]).toEqual([['Main.Emotion.Fleet1Value', 66]])
  })

  it('没有待落盘的草稿时照旧按快照播种', () => {
    // 上一条的对照组：同样的快照，没有草稿就该播种。
    const config = values({}, {
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 100),
    })
    config.General.PublicEmotion = {Tasks: 'Main', Fleet1Value: 119} as never
    const args = schema({General: {PublicEmotion: {Fleet1Value: {value: 119}}}})
    const {writes} = planEmotionWrites(config, args, ['Main'], {signature: '', emitted: ''})
    expect([...writes]).toEqual([['General.PublicEmotion.Fleet1Value', 100]])
  })

  it('校验失败的数字草稿不镜像到其他任务账本', () => {
    const config = values({Fleet1Value: 100, Fleet1Record: '2026-09-21 04:07:00'}, {
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 100),
    })
    const edits = {'General.PublicEmotion.Fleet1Value': {value: '1.5', error: '请输入整数'}}
    const {writes} = planEmotionWrites(config, undefined, ['Main'], {signature: '', emitted: ''}, edits)
    expect([...writes]).toEqual([])
  })

  it('任务心情与现值相同时不重复播种', () => {
    // 接管结果与字段现值一样，写它什么也改不了（值仍等于模板默认值），
    // 却会和用户正在输入的草稿抢同一个字段。
    const config = values({}, {
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 119),
    })
    config.General.PublicEmotion = {Tasks: 'Main', Fleet1Value: 119} as never
    const args = schema({General: {PublicEmotion: {Fleet1Value: {value: 119}}}})
    const {writes} = planEmotionWrites(config, args, ['Main'], {signature: '', emitted: ''})
    expect([...writes]).toEqual([])
  })

  it('面板渲染的舰队与播种覆盖的舰队一致', () => {
    // 两条链路分别用 fleetUsageOf / minimumMoraleOf，若口径不一致，
    // 会出现「面板渲染了某支舰队但从未被播种」的空白账本。
    const config = values({}, {
      Main: moraleTask(4, 3, 'fleet1_mob_fleet2_boss', 120, 90),
      Event: moraleTask(5, 0, 'fleet1_all_fleet2_standby', 70),
    })
    const rendered = fleetUsageOf(config, undefined, ['Main', 'Event']).map(item => item.fleet)
    const seeded = Object.keys(minimumMoraleOf(config, undefined, ['Main', 'Event'])).map(Number).sort((a, b) => a - b)
    expect(rendered).toEqual([3, 4, 5])
    expect(seeded).toEqual([3, 4, 5])
  })
})

describe('去除监控任务：只停止参考，不改写共用心情', () => {
  /** 带心情值的任务设置。 */
  const moraleTask = (fleet1: number, fleet2: number, order: string,
                      fleet1Value: number, fleet2Value = 119) => ({
    Fleet: {Fleet1: fleet1, Fleet2: fleet2, FleetOrder: order},
    Emotion: {Fleet1Value: fleet1Value, Fleet2Value: fleet2Value},
  })

  const sharedKey = (fleet: number) => `General.PublicEmotion.Fleet${fleet}Value`

  /** 同「初始接管」分组：一串清单变化共用一个写入计划状态。 */
  const session = (
    tasks: Record<string, ReturnType<typeof moraleTask>>,
    shared: Record<number, number> = {},
  ) => {
    const draft = values({}, {...tasks})
    let state = {signature: '', emitted: ''}
    const sync = (list: string[]) => {
      const general = (draft.General.PublicEmotion ?? {}) as Record<string, unknown>
      draft.General.PublicEmotion = {Tasks: list.join(', '), ...general} as never
      for (const [fleet, value] of Object.entries(shared)) general[`Fleet${fleet}Value`] = value
    }
    return (list: string[]) => {
      sync(list)
      const {writes, state: next} = planEmotionWrites(draft, undefined, list, state)
      state = next
      for (const [path, value] of writes) {
        const [owner, group, argument] = path.split('.')
        ;(draft[owner][group] as Record<string, unknown>)[argument] = value
        if (owner === 'General') shared[Number(argument.match(/\d+/)?.[0])] = Number(value)
      }
      return [...writes].map(([path, value]) => `${path}=${JSON.stringify(value)}`)
    }
  }

  it('去掉最低的那个任务不会把心情抬高', () => {
    // 本次修正的核心：移除只是"不再参考它"，不重取最小值。
    // 若重算，剩下的 150 会把心情改高，等于往乐观方向改。
    const change = session({
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 50),
      Main2: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 150),
    })
    expect(change(['Main', 'Main2'])).toContain(`${sharedKey(1)}=50`)
    expect(change(['Main2']).some(item => item.startsWith(sharedKey(1)))).toBe(false)
  })

  it('去掉较高的那个任务同样不改写', () => {
    const change = session({
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 50),
      Main2: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 150),
    })
    expect(change(['Main', 'Main2'])).toContain(`${sharedKey(1)}=50`)
    expect(change(['Main']).some(item => item.startsWith(sharedKey(1)))).toBe(false)
  })

  it('清空整份名单不改写，之后重新加入也不重算', () => {
    const change = session({
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 50),
      Main2: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 150),
    })
    expect(change(['Main', 'Main2'])).toContain(`${sharedKey(1)}=50`)
    expect(change([])).toEqual([])
    // 重新加入：舰队1 已被接管（值不是模板默认值），不再播种。
    expect(change(['Main2']).some(item => item.startsWith(sharedKey(1)))).toBe(false)
  })

  it('去掉一个任务但它的舰队仍被别的任务用着 → 不改写', () => {
    const change = session({
      Main: moraleTask(4, 0, 'fleet1_all_fleet2_standby', 60),
      Event: moraleTask(4, 0, 'fleet1_all_fleet2_standby', 140),
    })
    expect(change(['Main', 'Event'])).toContain(`${sharedKey(4)}=60`)
    expect(change(['Event']).some(item => item.startsWith(sharedKey(4)))).toBe(false)
  })

  it('去掉某支舰队的最后一个使用者 → 不改写，该舰队只是不再展示', () => {
    const change = session({
      Main: moraleTask(4, 3, 'fleet1_mob_fleet2_boss', 120, 90),
      Event: moraleTask(5, 0, 'fleet1_all_fleet2_standby', 70),
    })
    const first = change(['Main', 'Event'])
    expect(first).toContain(`${sharedKey(3)}=90`)
    expect(first).toContain(`${sharedKey(4)}=120`)
    expect(first).toContain(`${sharedKey(5)}=70`)
    // 只留 Event：舰队3/4 不再展示，但值保持不动。
    const next = change(['Event'])
    expect(next.some(item => item.startsWith(sharedKey(3)))).toBe(false)
    expect(next.some(item => item.startsWith(sharedKey(4)))).toBe(false)
  })

  it('移除后重新加入同一支舰队 → 不重新取最小值', () => {
    const change = session({
      Main: moraleTask(4, 0, 'fleet1_all_fleet2_standby', 60),
      Event: moraleTask(5, 0, 'fleet1_all_fleet2_standby', 70),
    })
    expect(change(['Main', 'Event'])).toContain(`${sharedKey(4)}=60`)
    expect(change(['Event']).some(item => item.startsWith(sharedKey(4)))).toBe(false)
    expect(change(['Main', 'Event']).some(item => item.startsWith(sharedKey(4)))).toBe(false)
  })

  it('移除后新增一支从未接管过的舰队 → 只播种新的那支', () => {
    const change = session({
      Main: moraleTask(4, 0, 'fleet1_all_fleet2_standby', 60),
      Event: moraleTask(5, 0, 'fleet1_all_fleet2_standby', 70),
    })
    expect(change(['Main'])).toContain(`${sharedKey(4)}=60`)
    const next = change(['Main', 'Event'])
    expect(next).toContain(`${sharedKey(5)}=70`)
    expect(next.some(item => item.startsWith(sharedKey(4)))).toBe(false)
  })

  it('任务把自己的舰队改到别的编号 → 新编号按最小值接管，旧编号不动', () => {
    const change = session({
      Main: moraleTask(4, 0, 'fleet1_all_fleet2_standby', 60),
      Moved: moraleTask(6, 0, 'fleet1_all_fleet2_standby', 70),
    })
    expect(change(['Main'])).toContain(`${sharedKey(4)}=60`)
    const next = change(['Main', 'Moved'])
    expect(next).toContain(`${sharedKey(6)}=70`)
    expect(next.some(item => item.startsWith(sharedKey(4)))).toBe(false)
  })
})

describe('moraleMirrorOf', () => {
  /** 带共用心情账本与任务图心情的配置。 */
  const mirrorValues = (
    shared: Record<string, unknown>, task: Record<string, unknown>,
  ) => values({Tasks: 'Main', ...shared}, {Main: task})

  it('按职能把共用值镜像到任务图对应的 Fleet1/Fleet2Value', () => {
    // Main 用真实舰队4 打道中、3 打 Boss，因此舰队4 写 Emotion.Fleet1Value。
    const config = mirrorValues(
      {Fleet4Value: 100, Fleet4Record: '2026-09-21 00:07:21', Fleet3Value: 88, Fleet3Record: '2026-09-21 00:07:21'},
      {
        Fleet: {Fleet1: 4, Fleet2: 3, FleetOrder: 'fleet1_mob_fleet2_boss'},
        Emotion: {Fleet1Value: 150, Fleet2Value: 119},
      },
    )
    expect([...moraleMirrorOf(config, undefined, ['Main'])]).toEqual([
      ['Main.Emotion.Fleet1Value', 100],
      ['Main.Emotion.Fleet2Value', 88],
    ])
  })

  it('同一真实舰队双队分工时同时镜像两个任务槽位', () => {
    const config = mirrorValues(
      {Fleet5Value: 40, Fleet5Record: '2026-09-21 00:07:21'},
      {
        Fleet: {Fleet1: 5, Fleet2: 5, FleetOrder: 'fleet1_mob_fleet2_boss'},
        Emotion: {Fleet1Value: 100, Fleet2Value: 90},
      },
    )
    expect([...moraleMirrorOf(config, undefined, ['Main'])]).toEqual([
      ['Main.Emotion.Fleet1Value', 40],
      ['Main.Emotion.Fleet2Value', 40],
    ])
  })

  it('不写任务图的记录时间：那些字段由程序维护，API 会拒写', () => {
    // 任务图的 Emotion.FleetNRecord 在 schema 里是 display: disabled，写它只会拿到
    // READ_ONLY（面板上挂一条红色写入错误）。记录时间由后端记账时按职能回写。
    const config = mirrorValues(
      {Fleet4Value: 100, Fleet4Record: '2026-09-21 00:07:21'},
      {
        Fleet: {Fleet1: 4, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'},
        Emotion: {Fleet1Value: 150},
      },
    )
    const paths = [...moraleMirrorOf(config, undefined, ['Main'])].map(([path]) => path)
    expect(paths).toEqual(['Main.Emotion.Fleet1Value'])
  })

  it('值已一致时不产生写入', () => {
    const config = mirrorValues(
      {Fleet1Value: 100, Fleet1Record: '2026-09-21 00:07:21'},
      {
        Fleet: {Fleet1: 1, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'},
        Emotion: {Fleet1Value: 100},
      },
    )
    expect([...moraleMirrorOf(config, undefined, ['Main'])]).toEqual([])
  })

  it('单队全清只写道中位', () => {
    const config = mirrorValues(
      {Fleet1Value: 100, Fleet1Record: '2026-09-21 00:07:21'},
      {
        Fleet: {Fleet1: 1, Fleet2: 3, FleetOrder: 'fleet1_all_fleet2_standby'},
        Emotion: {Fleet1Value: 150, Fleet2Value: 119},
      },
    )
    // 舰队3 没出击，它的 Emotion.Fleet2Value 不该被共用账本碰。
    expect([...moraleMirrorOf(config, undefined, ['Main'])]).toEqual([
      ['Main.Emotion.Fleet1Value', 100],
    ])
  })

  it('多个任务各自镜像到自己的字段', () => {
    const config = values(
      {Tasks: 'Main, Event'},
      {
        Main: {
          Fleet: {Fleet1: 4, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'},
          Emotion: {Fleet1Value: 150},
        },
        Event: {
          Fleet: {Fleet1: 1, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'},
          Emotion: {Fleet1Value: 130},
        },
      },
    )
    config.General.PublicEmotion = {Fleet4Value: 100, Fleet4Record: 'r4', Fleet1Value: 100, Fleet1Record: 'r1'} as never
    const writes = [...moraleMirrorOf(config, undefined, ['Main', 'Event'])]
    const of = (owner: string) => writes.filter(([path]) => path.startsWith(`${owner}.`))
      .map(([path, value]) => ({path, value}))
    // 记录时间（r4 / r1）不下发：任务图的记录时间字段由程序自己维护。
    expect(of('Main')).toEqual([
      {path: 'Main.Emotion.Fleet1Value', value: 100},
    ])
    expect(of('Event')).toEqual([
      {path: 'Event.Emotion.Fleet1Value', value: 100},
    ])
  })

  it('空名单不产生写入', () => {
    const config = mirrorValues({Fleet1Value: 100}, {})
    expect([...moraleMirrorOf(config, undefined, [])]).toEqual([])
  })
})

describe('planEmotionWrites 的幂等门', () => {
  /** 两个任务共用真实舰队1，Main2 的任务图心情还停在上一次的值。 */
  const build = (ledger: Record<string, unknown> = {}) => values(ledger, {
    Main: {Fleet: {Fleet1: 1, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'}, Emotion: {Fleet1Value: 130}},
    Main2: {Fleet: {Fleet1: 1, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'}, Emotion: {Fleet1Value: 150}},
  })

  /**
   * 一串渲染共用一个计划状态。`apply` 打开时把下发内容落地，
   * 等价于写入同步回 config——幂等门要挡住的正是这一轮重复求值。
   * 播种会把记录时间一起写下，因此也要带着 `now` 跑，否则舰队永远算"没接管"。
   */
  const planner = (config: Values) => {
    let state = {signature: '', emitted: ''}
    return (list: string[], shared?: number, apply = false) => {
      if (shared !== undefined) (config.General.PublicEmotion as Record<string, unknown>).Fleet1Value = shared
      const {writes, state: next} = planEmotionWrites(
        config, undefined, list, state, {}, {now: '2026-09-21 04:07:00'})
      state = next
      const items = [...writes].map(([path, value]) => `${path}=${JSON.stringify(value)}`)
      if (apply) {
        for (const [path, value] of writes) {
          const [owner, group, argument] = path.split('.')
          ;(config[owner][group] as Record<string, unknown>)[argument] = value
        }
      }
      return items
    }
  }

  it('同一批写入只下发一次（防止每次渲染都重写）', () => {
    const plan = planner(build())
    const list = ['Main', 'Main2']
    const first = plan(list, undefined, true)
    expect(first).toContain('General.PublicEmotion.Fleet1Value=130')
    expect(first).toContain('Main2.Emotion.Fleet1Value=130')
    // 写入落盘会换掉 values 的引用、再求值一次，这一轮必须什么都不发。
    expect(plan(list, undefined, true)).toEqual([])
  })

  it('共用值改走再改回原值时会重新镜像', () => {
    // 幂等门若记成"每个字段只发一次"，改回原值就再也发不出去：
    // 面板显示 130 而 Main2 的任务图永远停在 131，两边对不上。
    const plan = planner(build())
    const list = ['Main', 'Main2']
    expect(plan(list, 130, true)).toContain('Main2.Emotion.Fleet1Value=130')
    expect(plan(list, 131, true)).toContain('Main2.Emotion.Fleet1Value=131')
    expect(plan(list, 130)).toContain('Main2.Emotion.Fleet1Value=130')
  })

  it('监控清单变化后按新清单重新求值', () => {
    // 清单变了就不该再拿上一批写入做比对，否则换任务后的镜像会被漏掉。
    // 这里账本已经有记录时间（接管过了），因此不会有播种写入混进来。
    const plan = planner(build({Fleet1Value: 130, Fleet1Record: '2026-09-21 00:00:00'}))
    const list = ['Main', 'Main2']
    expect(plan(list)).toContain('Main2.Emotion.Fleet1Value=130')
    expect(plan(list)).toEqual([])
    expect(plan(['Main2'])).toContain('Main2.Emotion.Fleet1Value=130')
  })

  it('清单为空时不产生写入', () => {
    const plan = planner(build())
    expect(plan([])).toEqual([])
    expect(plan([], 130)).toEqual([])
  })
})

describe('isSeeded：账本记录时间就是接管标记', () => {
  const DEFAULT = '2020-01-01 00:00:00'

  it('记录时间还是模板默认值 → 本纪元没接管过', () => {
    expect(isSeeded(DEFAULT, DEFAULT)).toBe(false)
  })

  it('记录时间不是默认值 → 已接管', () => {
    expect(isSeeded('2026-09-21 00:35:06', DEFAULT)).toBe(true)
  })

  it('没有记录时间 → 没接管过', () => {
    expect(isSeeded(undefined, DEFAULT)).toBe(false)
    expect(isSeeded('', DEFAULT)).toBe(false)
    expect(isSeeded(null, DEFAULT)).toBe(false)
  })

  it('取不到模板默认值时按已接管处理（宁可漏播，也别反复覆盖）', () => {
    expect(isSeeded('2026-09-21 00:35:06', undefined)).toBe(true)
  })
})

describe('记录时间的变动', () => {
  const RECORD_DEFAULT = '2020-01-01 00:00:00'
  const LEDGER = schema({General: {PublicEmotion: Object.fromEntries([
    ...Array.from({length: 6}, (_, index) => [`Fleet${index + 1}Value`, {value: 119}]),
    ...Array.from({length: 6}, (_, index) => [`Fleet${index + 1}Record`, {value: RECORD_DEFAULT}]),
  ])}})

  /** 模拟面板：写入落地（等价于 config 同步回来），时间可以往前走。 */
  const ticker = (config: Values) => {
    let state = {signature: '', emitted: ''}
    return (now: string) => {
      const result = planEmotionWrites(config, LEDGER, ['Main'], state, {}, {now})
      state = result.state
      for (const [path, value] of result.writes) {
        const [owner, group, argument] = path.split('.')
        ;(config[owner][group] as Record<string, unknown>)[argument] = value
      }
      return [...result.writes].map(([path, value]) => `${path}=${JSON.stringify(value)}`)
    }
  }

  it('记录时间写进账本后，时间继续走也不会重复下发', () => {
    const config = values({}, {
      Main: {
        Fleet: {Fleet1: 1, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'},
        Emotion: {Fleet1Value: 130},
      },
    })
    const tick = ticker(config)
    const seeded = tick('2026-09-21 04:07:00')
    expect(seeded).toContain('General.PublicEmotion.Fleet1Value=130')
    expect(seeded).toContain('General.PublicEmotion.Fleet1Record="2026-09-21 04:07:00"')
    expect(config.General.PublicEmotion).toMatchObject({
      Fleet1Value: 130, Fleet1Record: '2026-09-21 04:07:00'})

    // 一分钟后重新求值：账本的记录时间已经不是模板默认值，说明接管过，
    // 于是既不重播也不镜像，一个字段都不再下发（否则 effect 每轮都会重写一遍）。
    expect(tick('2026-09-21 04:08:00')).toEqual([])
  })
})

describe('seedRecordTime', () => {
  it('取值到分钟，同一分钟内的整批写入内容稳定', () => {
    expect(seedRecordTime(new Date(2026, 8, 21, 4, 7, 33))).toBe('2026-09-21 04:07:00')
    expect(seedRecordTime(new Date(2026, 8, 21, 4, 7, 59))).toBe('2026-09-21 04:07:00')
    expect(seedRecordTime(new Date(2026, 11, 5, 23, 59, 1))).toBe('2026-12-05 23:59:00')
  })
})

describe('开启 / 新增任务：两条播种路径各不相同', () => {
  const RECORD_DEFAULT = '2020-01-01 00:00:00'
  const NOW = '2026-09-21 04:07:00'

  /** 与真实 schema 同形的最小定义：FleetNValue 默认 119、FleetNRecord 默认 2020-01-01。 */
  const LEDGER_SCHEMA = schema({General: {PublicEmotion: Object.fromEntries([
    ...Array.from({length: 6}, (_, index) => [`Fleet${index + 1}Value`, {value: 119}]),
    ...Array.from({length: 6}, (_, index) => [`Fleet${index + 1}Record`, {value: RECORD_DEFAULT}]),
  ])}})

  const moraleTask = (fleet1: number, fleet2: number, order: string,
                      fleet1Value: number, fleet2Value = 119) => ({
    Fleet: {Fleet1: fleet1, Fleet2: fleet2, FleetOrder: order},
    Emotion: {Fleet1Value: fleet1Value, Fleet2Value: fleet2Value},
  })

  /** 模拟面板：同一份草稿上连续求值，写出去的内容落回草稿（等价于 config 同步回来）。 */
  const panel = (config: Values) => {
    let state = {signature: '', emitted: ''}
    return (list: string[], options: {fresh?: boolean} = {}) => {
      ;(config.General.PublicEmotion as Record<string, unknown>).Tasks = list.join(', ')
      const {writes, state: next} = planEmotionWrites(
        config, LEDGER_SCHEMA, list, state, {}, {...options, now: NOW})
      state = next
      for (const [path, value] of writes) {
        const [owner, group, argument] = path.split('.')
        ;(config[owner][group] as Record<string, unknown>)[argument] = value
      }
      return [...writes].map(([path, value]) => `${path}=${JSON.stringify(value)}`)
    }
  }

  it('开启时按当前清单重新播种：涉及的舰队一律重取最小值并写下记录时间', () => {
    // 账本里本来就是上一轮留下的 100，开启等于从零开始，按当前任务重取最小值。
    const config = values({Fleet1Value: 100, Fleet1Record: '2026-09-20 00:00:00'}, {
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 130),
      Main2: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 150),
    })
    const writes = panel(config)(['Main', 'Main2'], {fresh: true})
    expect(writes).toContain('General.PublicEmotion.Fleet1Value=130')
    expect(writes).toContain('General.PublicEmotion.Fleet1Record="2026-09-21 04:07:00"')
    // 停在 150 的那张任务图同时被镜像到 130。
    expect(writes).toContain('Main2.Emotion.Fleet1Value=130')
  })

  it('开启时把清单没用到的舰队标记清掉，免得以后加入任务时拿旧账本', () => {
    const config = values({
      Fleet4Value: 120, Fleet4Record: '2026-09-20 10:00:00',
      Fleet2Record: RECORD_DEFAULT,
    }, {
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 130),
    })
    const writes = panel(config)(['Main'], {fresh: true})
    expect(writes).toContain('General.PublicEmotion.Fleet4Record="2020-01-01 00:00:00"')
    // 标记本来就是默认值的舰队不用重复写。
    expect(writes.some(item => item.startsWith('General.PublicEmotion.Fleet2Record'))).toBe(false)
  })

  it('清单为空时开启：只清标记，不播种', () => {
    const config = values({Fleet4Value: 120, Fleet4Record: '2026-09-20 10:00:00'}, {})
    const writes = panel(config)([], {fresh: true})
    expect(writes).toContain('General.PublicEmotion.Fleet4Record="2020-01-01 00:00:00"')
    expect(writes.some(item => item.includes('Value='))).toBe(false)
  })

  it('开启中新增任务：只播种新出现的舰队，已有账本一个字段都不动', () => {
    const config = values({Fleet1Value: 130, Fleet1Record: '2026-09-21 00:00:00'}, {
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 130),
      Event: moraleTask(3, 0, 'fleet1_all_fleet2_standby', 90),
    })
    const plan = panel(config)
    expect(plan(['Main'])).toEqual([])
    const writes = plan(['Main', 'Event'])
    expect(writes).toContain('General.PublicEmotion.Fleet3Value=90')
    expect(writes).toContain('General.PublicEmotion.Fleet3Record="2026-09-21 04:07:00"')
    expect(writes.some(item => item.startsWith('General.PublicEmotion.Fleet1'))).toBe(false)
  })

  it('播种时记录时间一起写，任务图不接受模板默认的记录时间', () => {
    // 只写 Value 的话，后端会从模板默认（2020-01-01）推算恢复量、把心情顶到上限；
    // 任务图那边也一样——所以播种必须把账本的记录时间一起写下，而任务图的记录时间
    // 字段（API 拒写）则一个都不下发。
    const config = values({}, {
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 100),
      Main2: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 130),
    })
    const writes = panel(config)(['Main', 'Main2'])
    expect(writes).toContain('General.PublicEmotion.Fleet1Record="2026-09-21 04:07:00"')
    expect(writes).toContain('Main2.Emotion.Fleet1Value=100')
    expect(writes.some(item => item.includes('.Emotion.Fleet1Record'))).toBe(false)
    expect(writes.some(item => item.includes('2020-01-01'))).toBe(false)
  })

  it('被监听的必须真的记账：不记账的心情模式与沉船忽略都会被掰回来', () => {
    // 这两项都会让共用账本静默偏高，骗到共用同一支舰队的其它任务。
    const config = values({}, {
      Main: {Fleet: {Fleet1: 1, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'},
        Emotion: {Mode: 'ignore', IgnoreShipwreck: true}},
      Main2: {Fleet: {Fleet1: 1, Fleet2: 0, FleetOrder: 'fleet1_all_fleet2_standby'},
        Emotion: {Mode: 'calculate', IgnoreShipwreck: false}},
    })
    const writes = panel(config)(['Main', 'Main2'])
    expect(writes).toContain('Main.Emotion.Mode="calculate"')
    expect(writes).toContain('Main.Emotion.IgnoreShipwreck=false')
    // 本来就正常的任务一个字段都不动。
    expect(writes.some(item => item.startsWith('Main2.Emotion.Mode'))).toBe(false)
    expect(writes.some(item => item.startsWith('Main2.Emotion.IgnoreShipwreck'))).toBe(false)
  })

  it('记录时间还是模板默认值就算没接管过——不看值本身', () => {
    const config = values({Fleet1Value: 100, Fleet1Record: RECORD_DEFAULT}, {
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 130),
    })
    const writes = panel(config)(['Main'])
    expect(writes).toContain('General.PublicEmotion.Fleet1Value=130')
    expect(writes).toContain('General.PublicEmotion.Fleet1Record="2026-09-21 04:07:00"')
  })

  it('有记录时间就不再播种：即使值还是模板默认值，也以账本为准', () => {
    const config = values({Fleet1Value: 119, Fleet1Record: '2026-09-21 00:00:00'}, {
      Main: moraleTask(1, 0, 'fleet1_all_fleet2_standby', 130),
    })
    const writes = panel(config)(['Main'])
    expect(writes.some(item => item.startsWith('General.PublicEmotion'))).toBe(false)
    // 反而是任务图被账本的 119 顶掉。
    expect(writes).toContain('Main.Emotion.Fleet1Value=119')
  })
})

describe('taskCalculatesMorale', () => {
  const task = (mode: unknown) => ({Main: {Emotion: {Mode: mode}}})

  it('心情模式含「计算心情消耗」时算心情', () => {
    expect(taskCalculatesMorale(values({}, task('calculate')), undefined, 'Main')).toBe(true)
    // 「计算心情消耗 + 无视红脸出击警告」照样记账。
    expect(taskCalculatesMorale(values({}, task('calculate_ignore')), undefined, 'Main')).toBe(true)
  })

  it('只选「无视红脸出击警告」时不算心情', () => {
    expect(taskCalculatesMorale(values({}, task('ignore')), undefined, 'Main')).toBe(false)
  })

  it('读不到设置时按"会计算"处理，避免误报', () => {
    expect(taskCalculatesMorale(values({}, {}), undefined, 'Main')).toBe(true)
    expect(taskCalculatesMorale(values({}, task('')), undefined, 'Main')).toBe(true)
  })
})

describe('monitorSignature', () => {
  it('名单变化会改变签名', () => {
    expect(monitorSignature(['Main'])).not.toBe(monitorSignature(['Main', 'Event']))
  })

  it('名单不变时签名稳定', () => {
    expect(monitorSignature(['Main', 'Event'])).toBe(monitorSignature(['Main', 'Event']))
  })

  it('只看名单，不看任务设置', () => {
    // 被监听任务的上场舰队与职能在界面上是锁住的，签名跟着任务设置走只会把两个
    // 界面重新耦合起来；计划要不要重发由写入内容本身决定。
    expect(monitorSignature(['Main'])).toBe(monitorSignature(['Main']))
  })
})
