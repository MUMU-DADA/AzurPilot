import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { CalendarClock, Clock3, ListTree, Play, Search, Settings2, Ship, Terminal } from 'lucide-react'
import { api } from '../api/client'
import type { Config, Value } from '../api/types'
import { useApp, useConnection } from '../app/context'
import { usesLegacyLayout } from '../app/theme'
import { readRailView, setRailView, subscribeRailView } from '../app/railPrefs'
import { Empty, ErrorBox, Loading, Modal, PageTitle } from '../components/ui'
import { LogPanel } from '../components/LogPanel'
import { MeowfficerScorePanel } from '../components/MeowfficerScorePanel'
import { FieldInput } from '../components/FieldInput'
import { RestrictedLuaEditor } from '../components/RestrictedLuaEditor'
import { SharedEmotionPanel } from '../components/SharedEmotionPanel'
import { ShopStrategyHelp } from '../components/ShopStrategyHelp'
import { StorageField } from '../components/StorageField'
import { SchedulerWidget } from '../components/SchedulerWidget'
import { TaskQueue } from '../components/TaskQueue'
import { useInstanceOverview } from '../components/useInstanceOverview'
import { editor, prepareValue } from '../config/editors'
import { EditStatus } from '../components/EditStatus'
import { isFieldVisible } from './configVisibility'
import { managedBySharedEmotion, parseParticipatingTasks } from './sharedEmotion'

/**
 * 锁定提示文案：把 `{panel}` 换成跳转到共用心情面板的链接。
 *
 * 提示里点名"由哪里管理"，那就得能一键跳过去，不然用户还得自己找任务与分组。
 */
function SharedHint({text, instance}: {text: string; instance: string}) {
  const {ui} = useApp()
  if (!text.includes('{panel}')) return <>{text}</>
  const [before, after = ''] = text.split('{panel}')
  return (
    <>
      {before}
      <Link to={`/i/${instance}/task/General?group=PublicEmotion`}>{ui('task.sharedEmotionPanelName')}</Link>
      {after}
    </>
  )
}

export function TaskConfig() {
  const {instance = '', task = ''} = useParams()
  const [searchParams] = useSearchParams()
  const {schema, t, ui, notify, language, theme} = useApp()
  const connection = useConnection()
  const railView = useSyncExternalStore(subscribeRailView, readRailView)
  // 只在真的切到调度器时才请求总览数据，否则这一页白拉一份队列。
  const [railData, setRailData] = useInstanceOverview(instance, railView === 'scheduler')
  const [config, setConfig] = useState<Config>()
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmRun, setConfirmRun] = useState(false)
  const [shopModeError, setShopModeError] = useState('')

  const queue = editor(`config:${instance}`)
  const {edits, storageError} = useSyncExternalStore(queue.subscribe, queue.getSnapshot)
  const legacy = usesLegacyLayout(theme)
  const reload = useCallback(async () => {
    try {
      const confirmed = queue.confirmed()
      setConfig(await api.request('config.get', {instance}))
      queue.reconcile(confirmed)
      setError('')
    } catch (error) {
      setError((error as Error).message)
    }
  }, [instance, queue])

  // 字段保存成功后用服务端回传的整份配置替换本地副本。
  useEffect(() => {
    queue.onSaved = data => setConfig(data as Config)
    return () => { queue.onSaved = undefined }
  }, [queue])

  useEffect(() => {
    if (connection !== 'ready') return
    let active = true
    const confirmed = queue.confirmed()
    void api.request('config.get', {instance}).then(value => {
      if (active) { setConfig(value); queue.reconcile(confirmed); setError('') }
    }).catch(error => { if (active) setError(error.message) })
    return () => { active = false }
  }, [connection, instance, task, queue])
  useEffect(() => { setShopModeError('') }, [instance, task])

  // 从别处的提示跳过来时（`?group=PublicEmotion`）把对应分组滚进视野；
  // 分组要等 schema 到位才渲染，因此这里每次渲染都试一次，滚过就不再滚。
  const scrolledGroup = useRef('')
  useEffect(() => {
    const group = searchParams.get('group')
    if (!group || scrolledGroup.current === group) return
    const node = document.getElementById(`group-${group}`)
    if (!node) return
    scrolledGroup.current = group
    node.scrollIntoView({block: 'start'})
  })

  /**
   * 把已保存的本地编辑同步进 config 草稿，让依赖配置值的界面即时跟随。
   *
   * 有功能是"改了开关就换一套设置"的（共用心情启用后接管心情设置），
   * 只靠挂载时那次 config.get 会停在一个过期的快照上，
   * 用户得手动刷新页面才能看到新面板。这里用保存回执就地更新，
   * 不重新拉取配置，避免与正在输入的编辑撞车。
   */
  useEffect(() => {
    const synced = new Set<string>()
    return queue.subscribe(() => {
      const {edits} = queue.getSnapshot()
      const fresh: [string, Value][] = []
      for (const [path, edit] of Object.entries(edits)) {
        const mark = `${path}:${edit.sequence}`
        if (edit.status !== 'saved' || synced.has(mark)) continue
        synced.add(mark)
        fresh.push([path, edit.value])
      }
      if (!fresh.length) return
      // 惰性 updater 会推迟到渲染阶段执行，那时局部变量已出作用域，
      // 因此把新值序列化成纯数据（路径 + 值）传进去，不引用任何闭包变量。
      const pending: [string, Value][] = JSON.parse(JSON.stringify(fresh))
      setConfig(current => {
        if (!current) return current
        let values = current.values
        for (const [path, value] of pending) {
          const [owner, group, argument] = path.split('.')
          if (!owner || !group || !argument) continue
          const previous = values[owner]?.[group] ?? {}
          values = {...values, [owner]: {...values[owner], [group]: {...previous, [argument]: value}}}
        }
        return {...current, values}
      })
    })
  }, [queue])

  async function run() {
    setBusy(true)
    try {
      await queue.settled()
      await api.request('tasks.run', {instance, task})
      notify(ui('task.started'))
    } catch (error) {
      setError((error as Error).message)
    } finally {
      setBusy(false)
      setConfirmRun(false)
    }
  }

  const groups = schema?.args[task]
  // 共用心情面板接管 PublicEmotion 组的心情字段。真实舰队多达 6 支，只有参与任务
  // 真正用到的那几支该出现，其余既不在面板里、也不该单列出来（等于给没出击的编队
  // 配心情）；同时面板渲染的舰队不能再走通用渲染，否则同一支会出现两份。
  //
  // 与"面板是否显示"分开判断：关闭共用心情时这些字段同样不渲染——配置里留着的是
  // 上一轮运行算出的账本值，平铺 6 支舰队 × 6 项只会让人以为还要在这里配心情。
  const visibleGroups = Object.entries(groups ?? {}).map(([group, fields]) => {
    const visible = Object.entries(fields).filter(([arg, field]) => {
      // 按真实舰队的字段归面板：启用时面板按用到的舰队分组展示，关闭时也不平铺。
      if (group === 'PublicEmotion' && task === 'General' && /^Fleet\d/.test(arg)) return false
      const edit = edits[`${task}.${group}.${arg}`]
      const value = edit?.status === 'saved' ? edit.value : config?.values[task]?.[group]?.[arg] ?? field.value
      return isFieldVisible(arg, field, value) && `${t(`${group}.${arg}.name`)} ${group}.${arg}`.toLowerCase().includes(search.toLowerCase())
    })
    return {group, visible}
  }).filter(({visible}) => visible.length)

  const tool = Object.values(schema?.menu ?? {}).some(group => group.page === 'tool' && group.tasks.includes(task))
  // 指挥喵评分保留参数卡（评分来源、截图目录等），报告面板挂在参数卡上方。
  const scorePanel = task === 'MeowfficerScore' ? <MeowfficerScorePanel instance={instance}/> : null
  const showConfigToolbar = task !== 'FleetInfo' && Boolean(groups) && (visibleGroups.length > 0 || Boolean(search))

  if (!config) return error ? <ErrorBox message={error} retry={reload} /> : <Loading />

  const modal = confirmRun && (
    <Modal title={ui('task.runTitle', {task: t(`Task.${task}.name`)})} onClose={() => setConfirmRun(false)}>
      <p>{ui('task.runWarning')}</p>
      <button className="button primary" disabled={busy} onClick={run}>
        <Play size={15} />{ui('task.confirmRun')}
      </button>
    </Modal>
  )

  const groupCards = <>{visibleGroups.map(({group, visible}) => (
    <section className="panel config-group" key={group} id={`group-${group}`}>
      <div className="panel-heading">
        <div>
          <span className="group-indicator" />
          <h2 data-text={t(`${group}._info.name`)}>{t(`${group}._info.name`)}</h2>
        </div>
      </div>
      {group === 'ShopAdvanced' && (
        <ShopStrategyHelp task={task} language={language}/>
      )}
      {visible.map(([arg, field]) => {
        const path = `${task}.${group}.${arg}`
        const edit = edits[path]
        const value = edit ? edit.value : config.values[task]?.[group]?.[arg] ?? field.value
        const label = t(`${group}.${arg}.name`)
        const help = t(`${group}.${arg}.help`)
        // 参与共用心情的任务，其舰队心情由「总览设置 - 共用心情」统一管理，
        // 这里只读展示并说明去处，避免两个地方改同一支舰队的心情。
        const sharedMonitored = parseParticipatingTasks(config.values).includes(task)
        const sharedLocked = managedBySharedEmotion(config.values, group, arg) && sharedMonitored
        // 两类提示都点名"由哪里管理"，面板名渲染成可点击的跳转链接。
        const sharedHint = group === 'Fleet'
          ? ui('task.sharedEmotionFleetLocked')
          : ui('task.sharedEmotionLocked')
        const readonly = sharedLocked || ['disabled', 'readonly'].includes(field.display ?? '') || ['storage', 'stored', 'state', 'lock'].includes(field.type)
        const restrictedLua = field.mode === 'restricted_lua'
        const shopMode = group === 'ShopAdvanced' && arg === 'Mode'
        // 「立刻运行」只对每个任务的调度时间有意义，其他时间字段（如仪表盘记录时间）不显示。
        const runNow = group === 'Scheduler' && arg === 'NextRun' && !readonly
        const isMultiline = ['textarea', 'task_priority', 'yaml', 'storage'].includes(field.type) || field.mode === 'yaml' || restrictedLua

        return (
          <div className={`field-row ${isMultiline ? 'field-row-multiline' : ''}`} key={arg}>
            <div className="field-label">
              <label htmlFor={path}>
                {label}
                {readonly && <span className="small-label">{ui('task.readonly')}</span>}
              </label>
              {help && help !== 'help' && help !== arg && <p>{help.replace(/<[^>]*>/g, '')}</p>}
              {/* 多行控件的提示跟标题同一行，浮在它右端。 */}
              {isMultiline && <EditStatus id={path} edit={edit} retry={queue.retry} queue={queue} />}
            </div>
            <div className="field-control">
              {field.type === 'storage' ? (
                <StorageField value={value} disabled={false} onClear={() => queue.change(path, {})} />
              ) : restrictedLua ? (
                <RestrictedLuaEditor
                  id={path}
                  value={String(value ?? '')}
                  draftKey={`${instance}.${path}`}
                  label={label}
                  disabled={readonly}
                  offline={connection !== 'ready'}
                  onCheck={script => api.request('shop_strategy.validate', {instance, task: task as 'EventShop' | 'ShopFrequent' | 'ShopOnce' | 'PrivateQuarters' | 'OpsiShop' | 'OpsiVoucher', script})}
                  onApply={async script => {
                    setConfig(await api.request('config.patch', {instance, changes: [{path, value: script}]}))
                    setShopModeError('')
                  }}
                />
              ) : (
                <FieldInput
                  id={path}
                  value={value}
                  mode={field.mode}
                  type={field.type === 'input' && typeof field.value === 'number' ? 'number' : field.type}
                  options={field.option}
                  disabled={readonly}
                  preserveText
                  invalid={edit?.status === 'error' || (shopMode && !!shopModeError)}
                  label={label}
                  translateOption={option => t(`${group}.${arg}.${option}`)}
                  onChange={next => {
                    if (shopMode && next === 'advanced') {
                      const script = String(config.values[task]?.ShopAdvanced?.Script ?? '')
                      if (!script.trim()) {
                        setShopModeError(ui('script.modeRequiresScript'))
                        return
                      }
                    }
                    if (shopMode) setShopModeError('')
                    const {payload, text, error} = prepareValue(next, field)
                    queue.change(path, text ?? next, payload, error)
                  }}
                />
              )}
              {runNow && (
                <div className="field-actions">
                  <button type="button" className="button subtle icon-only" aria-label={ui('task.runNow')} title={ui('task.runNow')} disabled={connection !== 'ready'}
                    onClick={() => {
                      // 按钮等同清空该字段：空时间按参数默认值提交，调度器下一轮即把任务视为待运行。
                      const {payload, text, error} = prepareValue('', field)
                      queue.change(path, text ?? '', payload, error)
                    }}><Play size={15}/></button>
                </div>
              )}
              {!isMultiline && (shopMode && shopModeError && !edit ? (
                <div id={`${path}-status`} className="edit-status edit-error" role="alert">{shopModeError}</div>
              ) : <EditStatus id={path} edit={edit} retry={queue.retry} queue={queue} />)}
              {sharedLocked && (
                <p className="shared-emotion-hint"><SharedHint text={sharedHint} instance={instance}/></p>
              )}
            </div>
          </div>
        )
      })}
      {/* 面板排在启用开关与监控清单之后：先决定"要不要用、管哪几个任务"，再看
          按真实舰队分组的心情设置；把各舰队的心情顶在最前面，总开关就沉到下面了。
          关闭共用心情时也挂载（组件自己不渲染内容）：它要看得见开关被打开的那一瞬间，
          开启等于从零开始监听当前清单，涉及的舰队要重新播种。 */}
      {group === 'PublicEmotion' && task === 'General' && (
        <SharedEmotionPanel values={config.values} schema={schema} queue={queue} edits={edits}/>
      )}
    </section>
  ))}    {search && !visibleGroups.length && <Empty icon={<Search size={26} />} title={ui('task.noConfigFound')}>{ui('task.tryOtherKeyword')}</Empty>}
  </>

  const hasGroups = task !== 'FleetInfo' && Boolean(groups) && visibleGroups.length > 0
  // 只有紧凑主题把搜索框并进左列（跳转栏下方），其余主题保持标题下方的原样。
  const condensed = theme === 'extreme'
  const groupCardsBlock = <div className="config-groups">{groupCards}</div>
  const groupNav = <nav className="group-nav">
    {visibleGroups.map(({group}) => (
      <a
        key={group}
        href={`#group-${group}`}
        onClick={event => {
          event.preventDefault()
          document.getElementById(`group-${group}`)?.scrollIntoView({behavior: 'smooth', block: 'start'})
        }}
      >
        {t(`${group}._info.name`)}
      </a>
    ))}
  </nav>

  // 有分组导航时，搜索框随导航一起放进左列（导航下方）；没有导航时才留在标题下方。
  // 右列默认是任务设置的锚点目录，点右上角切到调度器；两种视图共用同一个外壳。
  const railToggle = <button
    type="button"
    className="task-rail-toggle icon-button"
    aria-pressed={railView === 'scheduler'}
    aria-label={railView === 'scheduler' ? ui('nav.railDirectory') : ui('nav.railScheduler')}
    title={railView === 'scheduler' ? ui('nav.railDirectory') : ui('nav.railScheduler')}
    onClick={() => setRailView(railView === 'scheduler' ? 'directory' : 'scheduler')}
  >{railView === 'scheduler' ? <CalendarClock size={17}/> : <ListTree size={17}/>}</button>

  const rail = <aside className={`task-config-rail is-${railView}`} aria-label={railView === 'scheduler' ? ui('scheduler.rail') : ui('task.groupNav')}>
    {railView === 'scheduler'
      ? <div className="task-rail-scheduler">
          <SchedulerWidget instance={instance} data={railData} onData={setRailData} action={railToggle}/>
          <section className="rail-schedule" aria-label={ui('scheduler.plan')}>
            <div className="rail-section-heading">
              <div><Clock3 size={15}/><span>{ui('scheduler.plan')}</span></div>
              <span>{railData?.tasks.length ?? 0}</span>
            </div>
            <TaskQueue instance={instance} data={railData}/>
          </section>
        </div>
      : <div className="task-rail-directory">
          <div className="rail-section-heading">
            <div>{railToggle}<span>{ui('task.groupNav')}</span></div>
          </div>
          {groupNav}
        </div>}
  </aside>

  const configToolbar = showConfigToolbar && <div className="config-toolbar">
      <div className="input-icon">
        <Search size={17} />
        <input placeholder={ui('task.searchConfigPlaceholder')} aria-label={ui('task.searchConfig')} value={search} onChange={event => setSearch(event.target.value)} />
      </div>
    </div>

  const head = <>
    {error && <ErrorBox message={error} retry={reload} />}
    {storageError && <ErrorBox message={storageError} />}
    {(!hasGroups || !condensed) && configToolbar}
  </>

  const groupsSection = task === 'FleetInfo' ? (
    <FleetInfo value={config.values.FleetInfo?.FleetInfo?.Result} />
  ) : !hasGroups ? (
    (search || !tool) && <Empty icon={<Settings2 size={30} />} title={ui('task.noConfig')}>
      {search ? ui('task.tryOtherKeyword') : ui('task.viewRelated')}
    </Empty>
  ) : groupCardsBlock

  const toolPanel = tool && <section className="panel tool-log-panel" aria-label={ui('monitor.logs')}>
    <div className="panel-heading">
      <div><Terminal size={18}/><h2>{ui('monitor.logs')}</h2></div>
      <button className="button primary" onClick={() => setConfirmRun(true)} disabled={busy || connection !== 'ready'}>
        <Play size={16}/>{ui('task.runTool')}
      </button>
    </div>
    <LogPanel />
  </section>

  // 旧版的任务详细设置：参数卡在左、右列在「分组目录 / 调度器」之间切，页名由顶栏居中显示。
  if (legacy) return <>
    <div className="task-config-legacy">
      <h1 className="legacy-sr-title">{t(`Task.${task}.name`)}</h1>
      <div className="task-config-settings">
        {/* 换任务时重挂一次，让内容列的淡入重放。 */}
        <div className="task-config-settings-inner" key={task}>{head}{groupsSection}{scorePanel}{toolPanel}</div>
      </div>
      {/* 目录是逐页内容，跟着任务换；调度器是常驻的，换任务不重挂。 */}
      <div className="task-config-rail-slot" key={railView === 'directory' ? task : 'scheduler'}>{rail}</div>
    </div>
    {modal}
  </>

  return <>
    {/* 紧凑主题下任务名与面包屑末段重复，省掉标题行让内容上移，只留无障碍标题 */}
    {theme === 'extreme'
      ? <h1 className="sr-title">{t(`Task.${task}.name`)}</h1>
      : <PageTitle title={t(`Task.${task}.name`)}/>}
    {head}
    {hasGroups ? <div className="config-layout">{groupNav}{groupCardsBlock}</div> : groupsSection}
    {/* 报告面板放在日志上方：先看完参数与运行入口，再看结果 */}
    {scorePanel}
    {toolPanel}
    {modal}
  </>
}

function FleetInfo({value}: {value: unknown}) {
  const {ui} = useApp()
  if (!value || (typeof value === 'object' && !Object.keys(value).length)) return <Empty icon={<Ship size={32}/>} title={ui('fleet.emptyTitle')}>{ui('fleet.emptyHint')}</Empty>
  let fleets: Record<string, Record<string, Array<{name: string; level?: number} | string>>>
  try {fleets = typeof value === 'string' ? JSON.parse(value) : value} catch {return <ErrorBox message={ui('fleet.invalid')}/>}
  const columns = {vanguard: ui('fleet.vanguard'), main: ui('fleet.main'), submarine: ui('fleet.submarine')}
  return <div className="fleet-grid">{[1, 2, 3, 4, 5, 6].map(fleet => <section className="panel" key={fleet}><div className="panel-heading"><h2>{ui('fleet.title', {number: fleet})}</h2><Ship size={18}/></div>{Object.entries(columns).map(([key, label]) => <div className="fleet-column" key={key}><h3>{label}</h3>{fleets[key]?.[fleet]?.length ? fleets[key][fleet].map((ship, index) => <div key={index}><span>{typeof ship === 'string' ? ship : ship.name}</span><small>{typeof ship !== 'string' && ship.level ? `Lv.${ship.level}` : ''}</small></div>) : <p>{ui('fleet.noRecord')}</p>}</div>)}</section>)}</div>
}
