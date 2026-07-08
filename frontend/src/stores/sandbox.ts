import { defineStore } from 'pinia'
import { http } from '@/api/http'
import type {
  AttachmentFile,
  ConversationResource,
  LlmResource,
  McpAdapterStatus,
  McpResource,
  PlaygroundAgentConfig,
  SandboxResource,
  SkillResource,
} from '@/api/types'

interface State {
  defaultLlm: LlmResource | null
  defaultSandbox: SandboxResource | null
  llms: LlmResource[]
  skills: SkillResource[]
  mcps: McpResource[]
  sandboxes: SandboxResource[]
  mcpAdapter: McpAdapterStatus
  agentConfig: PlaygroundAgentConfig | null
  conversations: ConversationResource[]
  activeConversationId: string
  attachments: AttachmentFile[]
  resourceVersion: number
}

export const useSandboxStore = defineStore('sandbox', {
  state: (): State => ({
    defaultLlm: null,
    defaultSandbox: null,
    llms: [],
    skills: [],
    mcps: [],
    sandboxes: [],
    mcpAdapter: { available: true, error: '' },
    agentConfig: null,
    conversations: [],
    activeConversationId: '',
    attachments: [],
    resourceVersion: 0,
  }),
  getters: {
    connectedMcps: (s) => s.mcps.filter((m) => ['connected', 'configured'].includes(m.status)),
    llmOptions: (s) => [s.defaultLlm, ...s.llms].filter(Boolean) as LlmResource[],
    sandboxOptions: (s) => [s.defaultSandbox, ...s.sandboxes].filter(Boolean) as SandboxResource[],
    activeConversation: (s) => s.conversations.find((c) => c.id === s.activeConversationId) || null,
  },
  actions: {
    async loadResources() {
      const { data } = await http.get('/playground/resources')
      this.defaultLlm = data.default_llm || null
      this.defaultSandbox = data.default_sandbox || null
      this.llms = data.llms || []
      this.skills = data.skills || []
      this.mcps = data.mcps || []
      this.sandboxes = data.sandboxes || []
      this.mcpAdapter = data.mcp_adapter || { available: true, error: '' }
      this.resourceVersion += 1
      return data as {
        default_llm: LlmResource
        default_sandbox: SandboxResource
        llms: LlmResource[]
        skills: SkillResource[]
        mcps: McpResource[]
        sandboxes: SandboxResource[]
        mcp_adapter: McpAdapterStatus
      }
    },
    async loadConversations() {
      const { data } = await http.get('/playground/conversations')
      this.conversations = data.conversations || []
      if (!this.activeConversationId || !this.conversations.some((c) => c.id === this.activeConversationId)) {
        this.activeConversationId = this.conversations[0]?.id || ''
      }
      return this.conversations
    },
    setActiveConversation(id: string) {
      if (this.conversations.some((c) => c.id === id)) {
        this.activeConversationId = id
      }
    },
    async createConversation(title = '') {
      const { data } = await http.post('/playground/conversations', { title })
      this.conversations = data.conversations || []
      this.activeConversationId = data.item?.id || this.conversations[0]?.id || ''
      this.resourceVersion += 1
      return data.item as ConversationResource
    },
    async deleteConversation(id: string) {
      const { data } = await http.delete(`/playground/conversations/${id}`)
      this.conversations = data.conversations || []
      if (this.activeConversationId === id || !this.conversations.some((c) => c.id === this.activeConversationId)) {
        this.activeConversationId = this.conversations[0]?.id || ''
      }
      this.resourceVersion += 1
    },
    async saveLlm(payload: { id?: string; name: string; model: string; base_url: string; api_key: string; temperature?: number }) {
      const { data } = await http.post('/playground/llms', payload)
      this.defaultLlm = data.default_llm || null
      this.defaultSandbox = data.default_sandbox || this.defaultSandbox
      this.llms = data.llms || []
      this.skills = data.skills || []
      this.mcps = data.mcps || []
      this.sandboxes = data.sandboxes || this.sandboxes
      this.mcpAdapter = data.mcp_adapter || this.mcpAdapter
      this.resourceVersion += 1
      await this.loadAgentConfig()
      return data.item as LlmResource
    },
    async deleteLlm(id: string) {
      const { data } = await http.delete(`/playground/llms/${id}`)
      this.defaultLlm = data.default_llm || null
      this.defaultSandbox = data.default_sandbox || this.defaultSandbox
      this.llms = data.llms || []
      this.skills = data.skills || []
      this.mcps = data.mcps || []
      this.sandboxes = data.sandboxes || this.sandboxes
      this.mcpAdapter = data.mcp_adapter || this.mcpAdapter
      this.resourceVersion += 1
      await this.loadAgentConfig()
    },
    async uploadSkill(form: FormData) {
      const { data } = await http.post('/playground/skills', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      this.defaultLlm = data.default_llm || null
      this.defaultSandbox = data.default_sandbox || this.defaultSandbox
      this.llms = data.llms || []
      this.skills = data.skills || []
      this.mcps = data.mcps || []
      this.sandboxes = data.sandboxes || this.sandboxes
      this.mcpAdapter = data.mcp_adapter || this.mcpAdapter
      this.resourceVersion += 1
      await this.loadAgentConfig()
      return data.item as SkillResource
    },
    async deleteSkill(id: string) {
      const { data } = await http.delete(`/playground/skills/${id}`)
      this.defaultLlm = data.default_llm || null
      this.defaultSandbox = data.default_sandbox || this.defaultSandbox
      this.llms = data.llms || []
      this.skills = data.skills || []
      this.mcps = data.mcps || []
      this.sandboxes = data.sandboxes || this.sandboxes
      this.mcpAdapter = data.mcp_adapter || this.mcpAdapter
      this.resourceVersion += 1
      await this.loadAgentConfig()
    },
    async saveSandbox(payload: { id?: string; name: string }) {
      const { data } = await http.post('/playground/sandboxes', payload)
      this.defaultLlm = data.default_llm || this.defaultLlm
      this.defaultSandbox = data.default_sandbox || this.defaultSandbox
      this.llms = data.llms || this.llms
      this.skills = data.skills || this.skills
      this.mcps = data.mcps || this.mcps
      this.sandboxes = data.sandboxes || []
      this.mcpAdapter = data.mcp_adapter || this.mcpAdapter
      this.resourceVersion += 1
      return data.item as SandboxResource
    },
    async installSandbox(id: string, agentConfig?: PlaygroundAgentConfig | null) {
      const { data } = await http.post(`/playground/sandboxes/${id}/install`, { agent_config: agentConfig || this.agentConfig })
      this.defaultLlm = data.default_llm || this.defaultLlm
      this.defaultSandbox = data.default_sandbox || this.defaultSandbox
      this.llms = data.llms || this.llms
      this.skills = data.skills || this.skills
      this.mcps = data.mcps || this.mcps
      this.sandboxes = data.sandboxes || []
      this.mcpAdapter = data.mcp_adapter || this.mcpAdapter
      this.resourceVersion += 1
      return data.item as SandboxResource
    },
    async deleteSandbox(id: string) {
      const { data } = await http.delete(`/playground/sandboxes/${id}`)
      this.defaultLlm = data.default_llm || this.defaultLlm
      this.defaultSandbox = data.default_sandbox || this.defaultSandbox
      this.llms = data.llms || this.llms
      this.skills = data.skills || this.skills
      this.mcps = data.mcps || this.mcps
      this.sandboxes = data.sandboxes || []
      this.mcpAdapter = data.mcp_adapter || this.mcpAdapter
      this.resourceVersion += 1
      await this.loadAgentConfig()
    },
    async saveMcp(payload: { id?: string; name: string; config: any }) {
      const { data } = await http.post('/playground/mcps', payload)
      this.defaultLlm = data.default_llm || null
      this.defaultSandbox = data.default_sandbox || this.defaultSandbox
      this.llms = data.llms || []
      this.skills = data.skills || []
      this.mcps = data.mcps || []
      this.sandboxes = data.sandboxes || this.sandboxes
      this.mcpAdapter = data.mcp_adapter || this.mcpAdapter
      this.resourceVersion += 1
      await this.loadAgentConfig()
      return data.item as McpResource
    },
    async deleteMcp(id: string) {
      const { data } = await http.delete(`/playground/mcps/${id}`)
      this.defaultLlm = data.default_llm || null
      this.defaultSandbox = data.default_sandbox || this.defaultSandbox
      this.llms = data.llms || []
      this.skills = data.skills || []
      this.mcps = data.mcps || []
      this.sandboxes = data.sandboxes || this.sandboxes
      this.mcpAdapter = data.mcp_adapter || this.mcpAdapter
      this.resourceVersion += 1
      await this.loadAgentConfig()
    },
    async loadAgentConfig() {
      const { data } = await http.get('/playground/agent-config')
      this.agentConfig = data
      return data as PlaygroundAgentConfig
    },
    async saveAgentConfig(config: PlaygroundAgentConfig) {
      const { data } = await http.put('/playground/agent-config', config)
      this.agentConfig = data
      return data as PlaygroundAgentConfig
    },
    async loadAttachments() {
      const { data } = await http.get('/playground/attachments')
      this.attachments = data.files || []
      return this.attachments
    },
    async clearAttachments() {
      await http.delete('/playground/attachments')
      await this.loadAttachments()
    },
  },
})
