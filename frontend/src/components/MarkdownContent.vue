<template>
  <div class="markdown-content" v-html="rendered" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { marked } from 'marked'

const props = defineProps<{ content?: string }>()

marked.setOptions({ gfm: true, breaks: true })

const rendered = computed(() => sanitizeHtml(String(marked.parse(props.content || ''))))

const allowedTags = new Set([
  'A', 'BLOCKQUOTE', 'BR', 'CODE', 'DEL', 'DETAILS', 'DIV', 'EM', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
  'HR', 'INPUT', 'LI', 'OL', 'P', 'PRE', 'S', 'STRONG', 'SUMMARY', 'TABLE', 'TBODY', 'TD', 'TH', 'THEAD',
  'TR', 'UL',
])

function sanitizeHtml(value: string) {
  const document = new DOMParser().parseFromString(value, 'text/html')
  for (const element of Array.from(document.body.querySelectorAll('*'))) {
    if (!allowedTags.has(element.tagName)) {
      element.replaceWith(...Array.from(element.childNodes))
      continue
    }
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase()
      const allowed = ['class', 'title', 'start'].includes(name)
        || (element.tagName === 'A' && name === 'href')
        || (element.tagName === 'INPUT' && ['type', 'checked', 'disabled'].includes(name))
      if (!allowed) element.removeAttribute(attribute.name)
    }
    if (element.tagName === 'A') {
      const href = element.getAttribute('href') || ''
      if (!/^(https?:|mailto:|#|\/)/i.test(href)) element.removeAttribute('href')
      element.setAttribute('target', '_blank')
      element.setAttribute('rel', 'noopener noreferrer')
    }
    if (element.tagName === 'INPUT') {
      if (element.getAttribute('type') !== 'checkbox') element.remove()
      else element.setAttribute('disabled', '')
    }
  }
  return document.body.innerHTML
}
</script>

<style scoped>
.markdown-content {
  min-width: 0;
  color: var(--text-main);
  font-size: 12.5px;
  line-height: 1.58;
  overflow-wrap: anywhere;
}

.markdown-content :deep(> :first-child) {
  margin-top: 0;
}

.markdown-content :deep(> :last-child) {
  margin-bottom: 0;
}

.markdown-content :deep(p),
.markdown-content :deep(ul),
.markdown-content :deep(ol),
.markdown-content :deep(blockquote),
.markdown-content :deep(pre),
.markdown-content :deep(table) {
  margin: 6px 0;
}

.markdown-content :deep(h1) {
  margin: 14px 0 6px;
  color: var(--text-strong);
  font-size: 16px;
  font-weight: 680;
  line-height: 1.35;
  letter-spacing: 0;
}

.markdown-content :deep(h2) {
  margin: 12px 0 5px;
  color: var(--text-strong);
  font-size: 14.5px;
  font-weight: 650;
  line-height: 1.4;
  letter-spacing: 0;
}

.markdown-content :deep(h3),
.markdown-content :deep(h4) {
  margin: 10px 0 5px;
  color: var(--text-strong);
  font-size: 13px;
  font-weight: 650;
  line-height: 1.45;
  letter-spacing: 0;
}

.markdown-content :deep(ul),
.markdown-content :deep(ol) {
  padding-left: 20px;
}

.markdown-content :deep(li + li) {
  margin-top: 2px;
}

.markdown-content :deep(a) {
  color: var(--accent);
  text-decoration: none;
}

.markdown-content :deep(a:hover) {
  text-decoration: underline;
}

.markdown-content :deep(code) {
  padding: 2px 5px;
  border: 1px solid var(--border-soft);
  border-radius: 4px;
  background: var(--surface-code);
  font-family: var(--font-mono);
  font-size: 0.9em;
}

.markdown-content :deep(pre) {
  max-width: 100%;
  overflow: auto;
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-code);
  line-height: 1.6;
}

.markdown-content :deep(pre code) {
  padding: 0;
  border: 0;
  background: transparent;
  white-space: pre;
}

.markdown-content :deep(blockquote) {
  padding: 2px 0 2px 11px;
  border-left: 2px solid var(--accent);
  color: var(--text-muted);
}

.markdown-content :deep(table) {
  display: block;
  width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
}

.markdown-content :deep(th),
.markdown-content :deep(td) {
  padding: 7px 9px;
  border: 1px solid var(--border);
  font-size: 11.5px;
  text-align: left;
}

.markdown-content :deep(th) {
  background: var(--surface-3);
  color: var(--text-strong);
}

.markdown-content :deep(hr) {
  border: 0;
  border-top: 1px solid var(--border);
}

@media (max-width: 600px) {
  .markdown-content {
    font-size: 14px;
    line-height: 1.6;
  }

  .markdown-content :deep(h1) {
    font-size: 17px;
  }

  .markdown-content :deep(h2) {
    font-size: 15.5px;
  }

  .markdown-content :deep(h3),
  .markdown-content :deep(h4) {
    font-size: 14px;
  }
}
</style>
