import type { Connection } from '../api/client'

/**
 * 资源统计页的自动刷新偏好。
 *
 * 统计页没有任何推送，数据只在筛选条件变化或手动点「刷新统计」时才重新拉取；
 * 这里保存的间隔让页面自己按节奏重拉，选中的档位跨会话沿用。
 * 0 表示关闭，也是默认值——自动刷新只应由用户主动开启。
 */
export const AUTO_REFRESH_OPTIONS = [0, 5, 30, 60, 300, 900] as const

export type AutoRefresh = (typeof AUTO_REFRESH_OPTIONS)[number]

const STORAGE_KEY = 'azurpilot.statistics.auto-refresh'

export function isAutoRefresh(value: unknown): value is AutoRefresh {
    return typeof value === 'number' && (AUTO_REFRESH_OPTIONS as readonly number[]).includes(value)
}

export function readAutoRefresh(): AutoRefresh {
    try {
        const saved = Number(localStorage.getItem(STORAGE_KEY))
        return isAutoRefresh(saved) ? saved : 0
    } catch { return 0 }
}

export function writeAutoRefresh(seconds: AutoRefresh) {
    try { localStorage.setItem(STORAGE_KEY, String(seconds)) } catch { /* 存储不可用时仅当前会话生效。 */ }
}

/**
 * 是否应当计时。三种情况不必重拉：用户没开启、连接没就绪（请求会直接被拒）、
 * 页面在后台——桌面端窗口常被最小化几小时，按固定间隔拉整页统计纯属浪费。
 */
export function shouldAutoRefresh(seconds: number, connection: Connection, pageHidden: boolean): boolean {
    return seconds > 0 && connection === 'ready' && !pageHidden
}
