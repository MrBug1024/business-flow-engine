export interface User {
  id: string
  email: string
  name: string
  avatar: string
  provider: string
}

export interface TableMeta {
  table_name: string
  role: string
  role_confirmed?: boolean
  row_count: number
  col_count: number
  columns?: any[]
}

export interface Scenario {
  id: string
  name: string
  description: string
  owner_id: string
  status: string
  created_at: number
  updated_at: number
  tables_meta: TableMeta[]
  trace_chain?: any
  relations?: any
  flow?: any
  outputs?: any[]
  skills?: any[]
}

export interface ToolTrace {
  name: string
  args_summary?: string
  result_summary?: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  tools?: ToolTrace[]
  created_at?: number
}

export interface ClarifyQuestion {
  id: string
  question: string
  options: string[]
  allow_custom: boolean
  multi_select: boolean
}

export interface Interaction {
  type: string
  title: string
  context: string
  questions: ClarifyQuestion[]
}

export interface CapabilityItem {
  scenario_id: string
  namespace: string
  display_name: string
  summary: string
  when_to_use: string[]
  not_for: string[]
  tools: string[]
  skill_available?: boolean
  mcp_available?: boolean
  mounted: boolean
}

export interface SkillResource {
  id: string
  name: string
  skill_name?: string
  description?: string
  dependencies?: {
    python?: string[]
    node?: Record<string, string>
    files?: string[]
    compatibility?: string
  }
  warnings?: string[]
  path?: string
  created_at?: number
}

export interface McpResource {
  id: string
  name: string
  status: 'connected' | 'configured' | 'error' | string
  tools: string[]
  error?: string
  config?: any
  dependencies?: {
    python?: string[]
    node?: Record<string, string>
    files?: string[]
  }
  updated_at?: number
}

export interface SandboxResource {
  id: string
  name: string
  type: string
  status: 'new' | 'installing' | 'ready' | 'error' | string
  managed?: boolean
  storage_label?: string
  builtin?: boolean
  error?: string
  dependencies?: {
    python?: string[]
    node?: Record<string, string>
    skills?: string[]
    mcps?: string[]
    fingerprint?: string
  }
  created_at?: number
  updated_at?: number
  installed_at?: number
}

export interface LlmResource {
  id: string
  name: string
  model: string
  base_url: string
  api_key?: string
  api_key_set?: boolean
  temperature?: number
  builtin?: boolean
  updated_at?: number
}

export interface ConversationResource {
  id: string
  title: string
  created_at: number
  updated_at: number
  message_count: number
}

export interface McpAdapterStatus {
  available: boolean
  error?: string
}

export interface AgentUnitConfig {
  id?: string
  name: string
  system_prompt: string
  llm_id?: string
  sandbox_id?: string
  enabled_skills: string[]
  enabled_mcps: string[]
}

export interface PlaygroundAgentConfig {
  main_agent: AgentUnitConfig & {
    default_system_prompt?: string
    enabled_subagents?: string[]
  }
  subagents: AgentUnitConfig[]
}

export interface AttachmentFile {
  name: string
  size: number
  path?: string
}
