import type { Page } from "playwright"
export const SCENARIO_NAMES: string[]
export function openScenario(page: Page, baseURL: string, name: string, opts?: { tab?: string; query?: string }): Promise<void>
