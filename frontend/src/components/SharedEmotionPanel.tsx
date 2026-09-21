import { useEffect, useRef } from 'react'
import type { Edit } from '../config/EditQueue'
import type { Field, Schema, Value, Values } from '../api/types'
import { useApp } from '../app/context'
import { EditStatus } from './EditStatus'
import { FieldInput } from './FieldInput'
import { prepareValue } from '../config/editors'
import {
  SLOT_FIELDS,
  fleetUsageOf,
  parseTasks,
  planEmotionWrites,
  seedRecordTime,
  sharedEmotionEnabled,
  taskCalculatesMorale,
  type EmotionWriteState,
} from '../pages/sharedEmotion'

interface Props {
  values: Values
  schema?: Schema
  /** 与任务设置页共用的搜索词；面板字段也应遵循同一套过滤语义。 */
  search?: string
  /** 与任务设置页共用的编辑队列，保证两处改动走同一套暂存与重试逻辑。 */
  queue: {change: (path: string, value: Value, payload?: Value, error?: string) => void; retry: () => void}
  edits: Record<string, Edit>
}

/**
 * 记录时间是记账基准（程序靠它推算恢复量），改了会让恢复量算错，因此只读。
 * 心情值可以手改：程序推算未必总对得上游戏实际，允许用户按看到的数值纠正。
 */
const READONLY_FIELDS = new Set(['Record'])

/**
 * 共用心情的「真实舰队」设置面板。
 *
 * 舰队清单由参与任务推导：只有任务真的会用到舰队2 时，才显示它，
 * 避免给一支根本不出击的编队配心情。
 * 字段读写的是 `General.PublicEmotion`，因此同一支真实舰队被多个任务
 * 复用时改一次即可，跨任务自动生效。
 *
 * 关闭共用心情时这个组件仍然挂载（只是不渲染内容）：它要看得见"开关被打开"这一
 * 瞬间——开启等于从零开始监听当前清单，涉及的舰队要全部重新播种。
 */
export function SharedEmotionPanel({values, schema, queue, edits, search = ''}: Props) {
  const {t, ui} = useApp()
  const enabled = sharedEmotionEnabled(values)
  const tasks = parseTasks(values?.General?.PublicEmotion?.Tasks)
  const usage = fleetUsageOf(values, schema, tasks)
  const query = search.trim().toLowerCase()
  const matches = (text: string) => !query || text.toLowerCase().includes(query)
  const planned = useRef<EmotionWriteState>({signature: '', emitted: ''})
  // undefined 表示首次渲染：刷新页面时开关本来就是开的，不该当成"刚开启"重播。
  const wasEnabled = useRef<boolean | undefined>(undefined)

  /** 按路径取字段定义并提交一次改动。 */
  const write = (path: string, value: Value) => {
    const [owner, group, argument] = path.split('.')
    const field: Field | undefined = schema?.args?.[owner]?.[group]?.[argument]
    if (!field) return
    const {payload, text, error} = prepareValue(value, field)
    queue.change(path, text ?? value, payload, error)
  }

  // 播种与镜像是**同一次计算**（planEmotionWrites）：两者原先各是一个 effect，
  // 在同一轮渲染里读到不同版本的输入——播种已算出新值、config 草稿还是旧值，
  // 于是镜像拿旧值把刚播种的值覆盖回去，最后各任务图参差不齐。
  // 现在每次渲染只求一次完整计划，只下发"还没发过"的写入，
  // 因此手改面板值也能立刻镜像到任务图（清单签名没变但值变了）。
  useEffect(() => {
    const fresh = enabled && wasEnabled.current === false
    wasEnabled.current = enabled
    if (!enabled) return
    const {writes, state} = planEmotionWrites(
      values, schema, tasks, planned.current, edits, {fresh, now: seedRecordTime()})
    planned.current = state
    for (const [path, value] of writes) write(path, value)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, values, schema, tasks, queue, edits])

  if (!enabled) return null

  if (!tasks.length) {
    return <p className="shared-emotion-empty">{ui('task.sharedEmotionNoTask')}</p>
  }

  // 心情模式不含「计算心情消耗」的任务：后端不会扣它的心情，账本会偏高，
  // 而共用账本偏高会同时骗到共用同一支舰队的其它任务。
  const blind = tasks.filter(task => !taskCalculatesMorale(values, schema, task, edits))

  return (
    <div className="shared-emotion-fleets">
      {blind.length > 0 && (
        <p className="shared-emotion-warning">
          {ui('task.sharedEmotionNoCalculateHint', {tasks: blind.join(' · ')})}
        </p>
      )}
      {usage.map(({fleet, tasks: users, roles}) => {
        const fleetTitle = ui('task.sharedEmotionFleetTitle', {fleet})
        const fleetScope = matches(`${fleetTitle} ${users.join(' ')}`)
        const fields = SLOT_FIELDS.map(suffix => {
          const argument = `Fleet${fleet}${suffix}`
          const field = schema?.args?.General?.PublicEmotion?.[argument]
          if (!field) return null
          const label = t(`PublicEmotion.${argument}.name`)
          const help = t(`PublicEmotion.${argument}.help`)
          return {suffix, argument, field, label, help}
        }).filter((field): field is NonNullable<typeof field> => Boolean(field))
        const visibleFields = fleetScope ? fields : fields.filter(({argument, label, help}) => matches(`General.PublicEmotion.${argument} ${label} ${help}`))
        if (!visibleFields.length) return null
        return <section className="shared-emotion-fleet" key={fleet}>
          <div className="shared-emotion-fleet-head">
            <h3>{fleetTitle}</h3>
            <span className="small-label">
              {roles.map(role => ui(role === 2 ? 'task.sharedEmotionRoleBoss' : 'task.sharedEmotionRoleMob')).join(' · ')}
            </span>
            <p>{users.map(task => (blind.includes(task)
              ? `${task}（${ui('task.sharedEmotionNoCalculate')}）`
              : task)).join(' · ')}</p>
          </div>
          {visibleFields.map(({suffix, argument, field, label, help}) => {
            const path = `General.PublicEmotion.${argument}`
            const edit = edits[path]
            const value: Value = edit ? edit.value
              : values?.General?.PublicEmotion?.[argument] ?? field.value
            const readonly = READONLY_FIELDS.has(suffix)
            return (
              <div className="field-row" key={argument}>
                <div className="field-label">
                  <label htmlFor={path}>
                    {label}
                    {readonly && <span className="small-label">{ui('task.readonly')}</span>}
                  </label>
                  {help && help !== 'help' && help !== argument && <p>{help}</p>}
                </div>
                <div className="field-control">
                  <FieldInput
                    id={path}
                    value={value}
                    mode={field.mode}
                    type={field.type === 'input' && typeof field.value === 'number' ? 'number' : field.type}
                    options={field.option}
                    disabled={readonly}
                    preserveText
                    invalid={edit?.status === 'error'}
                    label={label}
                    translateOption={option => t(`PublicEmotion.${argument}.${option}`)}
                    onChange={next => {
                      // 心情值在后端是整数，schema 的 input 类型没有携带这个约束。
                      const {payload, text, error} = prepareValue(next, suffix === 'Value' ? {...field, type: 'int'} : field)
                      queue.change(path, text ?? next, payload, error)
                      // API 在 Value 通过校验并落盘时原子刷新 Record。这里另发时间
                      // 会在数字被后端拒绝时仍改写基准，也可能覆盖更新的记账时间。
                    }}
                  />
                  <EditStatus id={path} edit={edit} retry={queue.retry} />
                </div>
              </div>
            )
          })}
        </section>
      })}
    </div>
  )
}
