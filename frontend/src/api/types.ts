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
  mounted: boolean
}
