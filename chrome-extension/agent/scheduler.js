/**
 * agent/scheduler.js
 * Scheduled task management using chrome.alarms.
 * Each task has a prompt and a run interval (minutes).
 * Run history is stored in chrome.storage.local, max last 24 hours.
 */

// ─── Storage keys ──────────────────────────────────────────────────────────────

const KEY_TASKS    = 'agentTasks'       // Array of task objects
const KEY_HISTORY  = 'agentHistory'     // Array of run records

const ALARM_PREFIX = 'agent_task_'
const HISTORY_TTL  = 24 * 60 * 60 * 1000  // 24 hours

// ─── Task schema ───────────────────────────────────────────────────────────────
// {
//   id: string (uuid-like),
//   name: string,
//   prompt: string,
//   intervalMinutes: number,   // 0 = manual only
//   enabled: boolean,
//   createdAt: number,
// }

// ─── History record schema ─────────────────────────────────────────────────────
// {
//   id: string,
//   taskId: string,
//   taskName: string,
//   prompt: string,
//   startedAt: number,
//   finishedAt: number,
//   success: boolean,
//   result: string,
//   iterations: number,
//   messages: object[],    // full conversation
// }

// ─── Tasks CRUD ────────────────────────────────────────────────────────────────

export async function getTasks() {
  const data = await chrome.storage.local.get([KEY_TASKS])
  return data[KEY_TASKS] || []
}

export async function saveTask(task) {
  const tasks = await getTasks()
  const idx = tasks.findIndex(t => t.id === task.id)
  if (idx >= 0) {
    tasks[idx] = task
  } else {
    tasks.push(task)
  }
  await chrome.storage.local.set({ [KEY_TASKS]: tasks })
  await syncAlarm(task)
  return task
}

export async function deleteTask(taskId) {
  const tasks = await getTasks()
  const filtered = tasks.filter(t => t.id !== taskId)
  await chrome.storage.local.set({ [KEY_TASKS]: filtered })
  await chrome.alarms.clear(`${ALARM_PREFIX}${taskId}`)
}

export function createTaskId() {
  return `task_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

// ─── Alarm sync ────────────────────────────────────────────────────────────────

async function syncAlarm(task) {
  const alarmName = `${ALARM_PREFIX}${task.id}`
  await chrome.alarms.clear(alarmName)

  if (task.enabled && task.intervalMinutes > 0) {
    chrome.alarms.create(alarmName, {
      delayInMinutes: task.intervalMinutes,
      periodInMinutes: task.intervalMinutes,
    })
    console.log(`[Scheduler] Alarm set: ${alarmName} every ${task.intervalMinutes}min`)
  }
}

/**
 * Re-register all alarms from storage. Call on service worker startup.
 */
export async function restoreAlarms() {
  const tasks = await getTasks()
  for (const task of tasks) {
    if (task.enabled && task.intervalMinutes > 0) {
      // Only create if not already registered
      const existing = await chrome.alarms.get(`${ALARM_PREFIX}${task.id}`)
      if (!existing) {
        await syncAlarm(task)
      }
    }
  }
  console.log(`[Scheduler] Restored alarms for ${tasks.length} tasks`)
}

/**
 * Handle a chrome.alarms.onAlarm event.
 * Returns the taskId if handled, null otherwise.
 */
export function getTaskIdFromAlarm(alarm) {
  if (!alarm.name.startsWith(ALARM_PREFIX)) return null
  return alarm.name.slice(ALARM_PREFIX.length)
}

export async function getTaskById(taskId) {
  const tasks = await getTasks()
  return tasks.find(t => t.id === taskId) || null
}

// ─── History management ────────────────────────────────────────────────────────

export async function getHistory() {
  const data = await chrome.storage.local.get([KEY_HISTORY])
  const all = data[KEY_HISTORY] || []
  // Filter to last 24h
  const cutoff = Date.now() - HISTORY_TTL
  return all.filter(r => r.startedAt >= cutoff)
}

export async function appendHistory(record) {
  const all = await getHistory()  // already filtered to 24h
  // Upsert: replace existing entry with same id, otherwise append
  const idx = all.findIndex(r => (r.id || r.sessionId) === (record.id || record.sessionId))
  if (idx >= 0) {
    all[idx] = record
  } else {
    all.push(record)
  }
  await chrome.storage.local.set({ [KEY_HISTORY]: all })
}

export async function clearHistory() {
  await chrome.storage.local.set({ [KEY_HISTORY]: [] })
}

export async function deleteHistory(id) {
  const all = await getHistory()
  await chrome.storage.local.set({ [KEY_HISTORY]: all.filter(r => (r.id || r.sessionId) !== id) })
}

export function createRunId() {
  return `run_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

// ─── Skill Memory ──────────────────────────────────────────────────────────────
// Skill schema:
// {
//   id: string,
//   name: string,          // short human-readable name, e.g. "发表推文"
//   site: string,          // optional domain hint, e.g. "twitter.com"
//   description: string,   // natural-language step-by-step description + key selectors
//   createdAt: number,
//   updatedAt: number,
// }

const KEY_SKILLS = 'agentSkills'

export async function getSkills() {
  const data = await chrome.storage.local.get([KEY_SKILLS])
  return data[KEY_SKILLS] || []
}

/**
 * Save or update a skill (upsert by id).
 * If id is omitted or not found, a new skill is created.
 */
export async function saveSkill({ id, name, site = '', description }) {
  const skills = await getSkills()
  const now = Date.now()
  const idx = id ? skills.findIndex(s => s.id === id) : -1

  if (idx >= 0) {
    skills[idx] = { ...skills[idx], name, site, description, updatedAt: now }
  } else {
    const newId = `skill_${now}_${Math.random().toString(36).slice(2, 8)}`
    skills.push({ id: newId, name, site, description, createdAt: now, updatedAt: now })
  }

  await chrome.storage.local.set({ [KEY_SKILLS]: skills })
  return idx >= 0 ? skills[idx] : skills[skills.length - 1]
}

export async function deleteSkill(skillId) {
  const skills = await getSkills()
  await chrome.storage.local.set({ [KEY_SKILLS]: skills.filter(s => s.id !== skillId) })
}

export async function getSkillById(skillId) {
  const skills = await getSkills()
  return skills.find(s => s.id === skillId) || null
}
