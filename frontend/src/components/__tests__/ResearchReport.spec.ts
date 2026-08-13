import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ResearchReport from '../ResearchReport.vue'

/**
 * ResearchReport renders the streamed report markdown and, critically, turns
 * the two citation markers emitted by the backend (`[cite:source]` and
 * `pqac-xxxxxxxx`) into visible, styled badges. This keeps the frontend
 * rendering contract aligned with the agent's source-marker output (manual
 * §5.7 component layer + Report Studio source-marker checks).
 */
describe('ResearchReport', () => {
  it('renders [cite:...] markers as a visible, verified citation badge', () => {
    const wrapper = mount(ResearchReport, {
      props: { report: 'Transformers rely on attention [cite:vaswani2017].', status: 'done' },
    })
    const badges = wrapper.findAll('.cite')
    expect(badges).toHaveLength(1)
    expect(badges[0].classes()).toContain('verified')
    expect(badges[0].text()).toBe('vaswani2017')
  })

  it('renders legacy pqac- markers as a citation badge', () => {
    const wrapper = mount(ResearchReport, {
      props: { report: 'See Mamba [pqac-abcd1234] for details.', status: 'done' },
    })
    const badges = wrapper.findAll('.cite')
    expect(badges).toHaveLength(1)
    expect(badges[0].text()).toBe('pqac-abcd1234')
  })

  it('renders markdown headings and list items', () => {
    const wrapper = mount(ResearchReport, {
      props: {
        report: '# Related Work\n- Attention [cite:a]\n- Mamba [cite:b]',
        status: 'done',
      },
    })
    const content = wrapper.find('.report-content')
    expect(content.find('h1').text()).toBe('Related Work')
    expect(content.findAll('li')).toHaveLength(2)
    expect(content.findAll('.cite')).toHaveLength(2)
  })

  it('shows the empty state when there is no report and the task is not running', () => {
    const wrapper = mount(ResearchReport, {
      props: { report: '', status: 'idle' },
    })
    expect(wrapper.text()).toContain('暂无报告')
  })

  it('shows the generating state when there is no report but status is running', () => {
    const wrapper = mount(ResearchReport, {
      props: { report: '', status: 'running' },
    })
    expect(wrapper.text()).toContain('正在生成综述')
  })
})
