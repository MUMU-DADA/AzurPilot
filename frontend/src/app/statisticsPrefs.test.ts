import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Connection } from '../api/client'
import { AUTO_REFRESH_OPTIONS, readAutoRefresh, shouldAutoRefresh, writeAutoRefresh } from './statisticsPrefs'

afterEach(() => vi.unstubAllGlobals())

describe('统计自动刷新偏好', () => {
  it('默认关闭，档位表的首项就是关闭', () => {
    vi.stubGlobal('localStorage', {getItem: () => null})
    expect(readAutoRefresh()).toBe(0)
    expect(AUTO_REFRESH_OPTIONS[0]).toBe(0)
  })
  it('恢复保存过的间隔档位', () => {
    vi.stubGlobal('localStorage', {getItem: () => '300'})
    expect(readAutoRefresh()).toBe(300)
  })
  it('接受 5 秒这个最低档位', () => {
    vi.stubGlobal('localStorage', {getItem: () => '5'})
    expect(readAutoRefresh()).toBe(5)
  })
  it('已去掉的 1 秒档位回退到关闭', () => {
    vi.stubGlobal('localStorage', {getItem: () => '1'})
    expect(readAutoRefresh()).toBe(0)
  })
  it('档位表按从小到大排列，最低的非关闭档位是 5 秒', () => {
    expect([...AUTO_REFRESH_OPTIONS].sort((a, b) => a - b)).toEqual([...AUTO_REFRESH_OPTIONS])
    expect(AUTO_REFRESH_OPTIONS[1]).toBe(5)
  })
  it('忽略不在档位表里的值', () => {
    vi.stubGlobal('localStorage', {getItem: () => '45'})
    expect(readAutoRefresh()).toBe(0)
    vi.stubGlobal('localStorage', {getItem: () => '每秒'})
    expect(readAutoRefresh()).toBe(0)
  })
  it('浏览器禁止存储时回退到关闭', () => {
    vi.stubGlobal('localStorage', {getItem: () => {throw new Error('存储不可用')}})
    expect(readAutoRefresh()).toBe(0)
  })
  it('写入选中的档位', () => {
    const saved = new Map<string, string>()
    vi.stubGlobal('localStorage', {setItem: (key: string, value: string) => saved.set(key, value)})
    writeAutoRefresh(60)
    expect([...saved.values()]).toEqual(['60'])
  })
})

describe('统计自动刷新的计时条件', () => {
  it('开启、已连接且页面可见时计时', () => {
    expect(shouldAutoRefresh(60, 'ready', false)).toBe(true)
  })
  it('关闭档位不计时', () => {
    expect(shouldAutoRefresh(0, 'ready', false)).toBe(false)
  })
  it('连接没就绪时不计时', () => {
    const offline: Connection[] = ['connecting', 'auth', 'offline']
    for (const connection of offline) {
      expect(shouldAutoRefresh(60, connection, false)).toBe(false)
    }
  })
  it('页面在后台时暂停计时', () => {
    expect(shouldAutoRefresh(60, 'ready', true)).toBe(false)
  })
})
