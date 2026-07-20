export type Language = 'zh' | 'en'

export type WorkspaceNode = {
  name: string
  path: string
  kind: 'file' | 'folder'
  icon?: string
  size?: number
  children?: WorkspaceNode[]
}

export type BusinessSummary = {
  id: string
  name: string
  current_version: number
  file_count: number
  open_question_count: number
}

export type BusinessResourceTarget = {
  businessId: string
  businessName: string
  root: boolean
  node: WorkspaceNode
}

export type BusinessResourceAction =
  | 'open'
  | 'new-file'
  | 'new-folder'
  | 'import'
  | 'export'
  | 'rename'
  | 'delete'

