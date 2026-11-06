// Exercise the shipped plain-ESM components, mocking only the host and hooks.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
const source = await readFile(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')
const uri = code => 'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
const env = globalThis.__findingsUI = { states: [], writes: [], queries: [], fixtures: {} }
const jsx = uri('export const jsx=(type,props,key)=>({type,props:props||{},key});export const jsxs=jsx;')
const react = uri('const e=globalThis.__findingsUI;export const useState=v=>[e.states.length?e.states.shift():typeof v==="function"?v():v,w=>e.writes.push(w)];export const useEffect=()=>{};export const useRef=v=>({current:v});')
async function load(sharedSelect = false) {
  const sdk = uri(`
    const e=globalThis.__findingsUI;
    export const ROUTES_AREA='routes',SIDEBAR_NAV_AREA='nav',PALETTE_AREA='palette';
    export const host={state:{profile:{get:()=> 'fixture'},connectionId:{get:()=>null}}};
    export const queryClient={};export const useValue=a=>a.get();
    export const useQuery=o=>{e.queries.push(o);return {data:o.enabled===false?undefined:e.fixtures[o.queryKey[2]],isLoading:false}};
    export const useMutation=()=>({mutate:v=>e.writes.push(v),isPending:false});
    ${sharedSelect ? "export const Select='sdk-select',SelectTrigger='sdk-trigger',SelectValue='sdk-value',SelectContent='sdk-content',SelectItem='sdk-item';" : ''}
  `)
  return import(uri(source.replaceAll("'@hermes/plugin-sdk'", JSON.stringify(sdk)).replaceAll("'react/jsx-runtime'", JSON.stringify(jsx)).replaceAll("'react'", JSON.stringify(react))))
}
const plugin = await load()
function nodes(n) {
  if (Array.isArray(n)) return n.flatMap(nodes)
  if (!n || typeof n !== 'object') return []
  if (typeof n.type === 'function') return nodes(n.type(n.props))
  return [n, ...nodes(n.props.children)]
}
function text(n) {
  if (Array.isArray(n)) return n.map(text).join(' ')
  if (n == null || typeof n === 'boolean') return ''
  if (typeof n !== 'object') return String(n)
  return text(typeof n.type === 'function' ? n.type(n.props) : n.props.children)
}
const finding = {
  findingId: 'hsf_fixture', title: 'A member can read another tenant’s invoice',
  severity: { level: 'high' }, evidenceState: 'source_supported', triage: { state: 'open' },
  target: { root: '/fixture/billing' }, locations: [{path:'src/invoices.ts',startLine:41,endLine:42,role:'sink'}],
  codeEvidence: [{path:'src/invoices.ts',startLine:40,endLine:43,code:'const account = input;\nconst result = load(account);\nreturn result;\n// end'}],
  summary: 'A request crosses the tenant boundary.', rootCause: 'Missing ownership check.',
  validation: {status:'NOT_RUN'}, counterEvidence:['A session is required.'], proofGaps:['Deployment policy was unavailable.'],
  remediation: {summary:'Constrain the query to the tenant.',regressionTests:['Deny a cross-tenant request.']}
}
function reset() {
  env.states=[];env.writes=[];env.queries=[];
  env.fixtures={ '/findings':{items:[finding],total:81}, '/repositories':{items:[{repoKey:'billing',root:'/fixture/billing'}]}, '/finding-folders':{items:[{folder:'src',count:81}]}, '/findings/hsf_fixture':finding, '/findings/hsf_fixture/patch':{content:'read-only patch'} }
}
test('select fallback preserves native values, disabled options and one decorative chevron', () => {
  const children=[{type:'option',props:{value:'',children:'Any severity'}},{type:'option',props:{value:'high',disabled:true,children:'High'}}]
  let changed
  const tree=nodes(plugin.SelectField({'aria-label':'Severity',value:'',children,onChange:e=>changed=e.target.value}))
  const select=tree.find(n=>n.type==='select')
  assert.equal(select.props.value,'')
  assert.equal(select.props['aria-label'],'Severity')
  assert.equal(tree.filter(n=>n.type==='svg').length,1)
  assert.equal(tree.find(n=>n.type==='option'&&n.props.value==='high').props.disabled,true)
  select.props.onChange({target:{value:'high'}})
  assert.equal(changed,'high')
})
test('shared SDK select handles empty values and grouped model IDs without rewriting them', async () => {
  const modern=await load(true)
  const exact='["provider","model/a:thinking"]'
  let changed
  const tree=nodes(modern.SelectField({'aria-label':'Model',value:exact,onChange:e=>changed=e.target.value,children:[
    {type:'option',props:{value:'',children:'Same as my chats'}},
    {type:'optgroup',props:{label:'Provider',children:[{type:'option',props:{value:exact,children:'Model A'}}]}}
  ]}))
  assert.equal(tree.some(n=>n.type==='select'),false)
  const root=tree.find(n=>n.type==='sdk-select'), entries=tree.filter(n=>n.type==='sdk-item')
  assert.ok(entries.every(n=>n.props.value!==''))
  assert.equal(root.props.value,entries[1].props.value)
  assert.match(text(tree),/Provider/)
  assert.ok(tree.some(n=>n.props.role==='group'&&n.props['aria-label']==='Provider'))
  root.props.onValueChange(entries[0].props.value);assert.equal(changed,'')
  root.props.onValueChange(entries[1].props.value);assert.equal(changed,exact)
  assert.equal(tree.filter(n=>n.type==='svg').length,0, 'SDK owns the trigger chevron')
})
test('numbered code highlights only source-declared locations and never invents line numbers', () => {
  assert.deepEqual(plugin.codeLines(finding.codeEvidence[0],finding.locations).map(x=>[x.number,x.highlighted]),[[40,false],[41,true],[42,true],[43,false]])
  assert.deepEqual(plugin.codeLines({...finding.codeEvidence[0],startLine:undefined},finding.locations).map(x=>x.number),[null,null,null,null])
  assert.ok(plugin.codeLines(finding.codeEvidence[0],[{path:'other.ts',startLine:40,endLine:43}]).every(x=>!x.highlighted))
  assert.equal(plugin.codeLines(finding.codeEvidence[0],finding.locations).map(x=>x.text).join('\n'),finding.codeEvidence[0].code)
})
test('untested candidates and inconclusive findings are not labeled code-supported or confirmed', () => {
  assert.equal(plugin.testResultText(finding),'Not tested: supported by reading the code.')
  for (const evidenceState of ['candidate','inconclusive','rejected']) {
    assert.doesNotMatch(plugin.testResultText({...finding,evidenceState}),/supported by reading|Confirmed by test/)
  }
  assert.match(plugin.testResultText({...finding,evidenceState:'runtime_confirmed',validation:{status:'runtime_confirmed',summary:'Test passed'}}),/Confirmed by test.*Test passed/)
})
test('finding rows expose proof strength separately from severity and triage', () => {
  const output=text(plugin.FindingRow({row:finding,onSelect(){}}))
  for (const value of ['high','Supported by code','Open','billing','src/invoices.ts:41']) assert.ok(output.includes(value),value)
})
test('table uses the same paginated request, preserves all filters, and shows server total', () => {
  reset();env.states=[{q:'invoice',status:'open',severity:'high',repo:'billing',folder:'src',evidenceState:'source_supported'},40]
  let selected
  const tree=nodes(plugin.BrowserView({kind:'Findings',view:'table',selected:null,setSelected:id=>selected=id,newScan:false,setNewScan(){}}))
  const request=env.queries.find(q=>q.queryKey[2]==='/findings'), params=new URLSearchParams(request.queryKey[3])
  for (const [key,value] of Object.entries({q:'invoice',status:'open',severity:'high',repo:'billing',folder:'src',evidenceState:'source_supported',offset:'40',limit:'40'})) assert.equal(params.get(key),value)
  assert.ok(tree.some(n=>n.type==='table'))
  assert.ok(tree.some(n=>n.props?.['aria-label']==='Folder'))
  assert.match(text(tree),/81/)
  const row=tree.find(n=>n.type==='button'&&text(n)===finding.title)
  row.props.onClick();assert.equal(selected,finding.findingId)
  const evidence=tree.find(n=>n.type==='select'&&n.props['aria-label']==='Evidence')
  evidence.props.onChange({target:{value:'candidate'}})
  assert.deepEqual(env.writes.at(-2)({q:'invoice',evidenceState:''}),{q:'invoice',evidenceState:'candidate'})
  assert.equal(env.writes.at(-1),0,'filter changes reset pagination')
})
test('finding sections preserve caveats, chain details, patches and notes', () => {
  reset()
  const tree=nodes(plugin.FindingDetail({id:finding.findingId,onBack(){}}))
  assert.deepEqual(tree.filter(n=>n.props.role==='tab').map(n=>text(n)),['Overview','Evidence','Remediation'])
  assert.equal(tree.filter(n=>n.props.role==='tabpanel').length,3)
  assert.equal(tree.filter(n=>n.props.role==='tabpanel'&&!n.props.hidden).length,1)
  const output=text(tree)
  for (const value of ['Deployment policy was unavailable.','A session is required.','read-only patch','Deny a cross-tenant request.','Save note']) assert.ok(output.includes(value),value)
  const evidence=tree.find(n=>n.props.role==='tab'&&text(n)==='Evidence')
  evidence.props.onClick();assert.equal(env.writes.at(-1),'evidence')
})
test('secret evidence never enters a code block or the suggested-patch section', () => {
  reset();env.fixtures['/findings/hsf_fixture']={...finding,secret:{type:'api key',fingerprint:'demo-fingerprint'},codeEvidence:[{code:'DO_NOT_RENDER_SECRET'}]}
  const output=text(plugin.FindingDetail({id:finding.findingId,onBack(){}}))
  assert.match(output,/demo-fingerprint/)
  assert.doesNotMatch(output,/DO_NOT_RENDER_SECRET|read-only patch/)
})
test('metadata-only index rows retain repository, path, proof state and triage', () => {
  const row=plugin.normalizeResponse({finding_id:'hsf_index',title:'Index only',root:'/fixture/billing',path:'src/invoices.ts',severity:'high',evidence_state:'source_supported',triage:{state:'accepted_risk'}})
  const output=text(plugin.FindingRow({row,onSelect(){}}))
  for(const value of ['billing','src/invoices.ts','Supported by code','Accepted risk']) assert.ok(output.includes(value),value)
  assert.doesNotMatch(output,/Location not recorded|src\/invoices\.ts:/)
})
test('a legacy dialog never opens a shared Select portal outside the native modal', async () => {
  reset()
  const modern=await load(true) // Select exists, but Dialog is absent on this SDK.
  const tree=nodes(modern.BrowserView({kind:'Findings',view:'table',selected:finding.findingId,setSelected(){},newScan:false,setNewScan(){}}))
  assert.ok(tree.some(n=>n.type==='dialog'))
  assert.ok(tree.some(n=>n.type==='select'&&n.props['aria-label']==='Finding status'))
  assert.ok(tree.some(n=>n.type==='sdk-trigger'&&n.props['aria-label']==='Severity'))
})
