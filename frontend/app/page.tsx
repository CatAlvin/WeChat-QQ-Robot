'use client';
/* eslint-disable @next/next/no-img-element -- authenticated blob URLs cannot use the Next image optimizer */

import { FormEvent, UIEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:8000/api/v1';
const WS_URL = API_BASE.replace(/^http/, 'ws') + '/ws';
const BEIJING_TIME_ZONE = 'Asia/Shanghai';
const CONVERSATION_PAGE_SIZE = 40;
const DYNAMIC_LIST_LIMIT = 50;

type Runtime = { global_mode:string; kill_switch:boolean; release_gate:string; simulation_accepted:boolean; shadow_accepted:boolean; live_time_window_enabled:boolean; live_auto_start:string; live_auto_end:string; media_storage_enabled:boolean; media_save_images:boolean; media_save_audio:boolean; media_save_files:boolean; media_ai_reply_enabled:boolean; media_max_file_mb:number; media_understanding_enabled:boolean; media_understand_images:boolean; media_transcribe_audio:boolean; media_extract_documents:boolean; media_vision_model:string; media_whisper_model:string; media_whisper_device:string; media_whisper_allow_download:boolean; media_understanding_max_chars:number; kimi_media_upload_enabled:boolean; kimi_media_upload_images:boolean; kimi_media_upload_videos:boolean; kimi_media_max_file_mb:number; tts_enabled:boolean; tts_reply_to_audio_only:boolean; tts_voice:string; tts_rate:number; tts_volume:number; tts_max_chars:number; napcat_quote_reply_enabled:boolean; updated_at:string };
type Dashboard = { runtime:Runtime; channels:Array<Record<string, unknown>>; metrics:Record<string, number>; recent_incidents:Incident[] };
type Contact = { id:string; platform:string; account_id?:string; platform_user_id:string; display_name:string; relationship_label:string; whitelisted:boolean; importance:string; ai_enabled:boolean; memory_enabled:boolean; style_profile:string; custom_prompt:string; reply_time_window_enabled?:boolean|null; reply_auto_start?:string|null; reply_auto_end?:string|null; media_storage_enabled?:boolean|null; media_ai_reply_enabled?:boolean|null; keepalive_enabled:boolean; keepalive_time:string; keepalive_account_id?:string; keepalive_last_sent_at?:string; keepalive_last_status:string; keepalive_last_content?:string; keepalive_last_error?:string; birthday_mmdd?:string|null; relationship_reminders_enabled:boolean; dormant_reminder_days:number; created_at:string };
type Group = { id:string; platform:string; account_id?:string; platform_group_id:string; display_name:string; allowed:boolean; ai_enabled:boolean };
type Conversation = { id:string; platform:string; account_id?:string; display_name:string; mode:string; managed_rounds:number; last_active_at:string; last_message:string; whitelisted:boolean; importance:string; ai_enabled:boolean; memory_enabled:boolean; model:string };
type MediaAttachment = { id:string; kind:string; segment_type:string; file_name:string; mime_type?:string; size_bytes?:number; status:string; error_code?:string; storage_source?:string; storage_attempts?:string[]; sha256?:string; local_path?:string; analysis_status:string; analysis_provider?:string; analysis_model?:string; analysis_text?:string; analysis_error_code?:string; analyzed_at?:string; download_url?:string };
type MediaPolicyInput = { storage_enabled:boolean; save_images:boolean; save_audio:boolean; save_files:boolean; ai_reply_enabled:boolean; max_file_mb:number; understanding_enabled:boolean; understand_images:boolean; transcribe_audio:boolean; extract_documents:boolean; vision_model:string; whisper_model:string; whisper_device:string; whisper_allow_download:boolean; understanding_max_chars:number; kimi_upload_enabled:boolean; kimi_upload_images:boolean; kimi_upload_videos:boolean; kimi_max_file_mb:number; tts_enabled:boolean; tts_reply_to_audio_only:boolean; tts_voice:string; tts_rate:number; tts_volume:number; tts_max_chars:number; napcat_quote_reply_enabled:boolean };
type MediaUnderstandingStatus = { enabled:boolean; vision:{service_online:boolean;model:string;model_installed:boolean}; speech:{package_installed:boolean;model_cached:boolean;model:string;device:string;download_allowed:boolean;model_directory:string}; documents:{plain_text:boolean;docx:boolean;pdf:boolean}; tts:{supported:boolean;engine:string;detail:string} };
type ChatMessage = { id:string; author:string; direction:string; sender_id?:string; receiver_id?:string; event_at:string; content:string; message_type:string; status:string; provider?:string; model?:string; created_at:string; attachments:MediaAttachment[] };
type ConversationSummary = { content:string; created_at?:string };
type Provider = { id:string; name:string; provider_type:string; base_url:string; model:string; credential_id?:string; enabled:boolean; priority:number; timeout_seconds:number };
type Credential = { id:string; label:string; provider:string; masked_hint:string; created_at:string };
type Memory = { id:string; contact_id:string; kind:string; content:string; review_status:string; pinned:boolean; expires_at?:string; created_at:string; updated_at:string };
type TaskItem = { id:string; kind:string; title:string; status:string; progress:number; attempts:number; max_attempts:number; error_detail?:string; result:Record<string,unknown>; created_at:string; updated_at:string };
type RecoveryItem = { id:string; source_type:string; source_id:string; kind:string; status:string; error_code?:string; error_detail?:string; retry_count:number; created_at:string };
type KnowledgeDoc = { id:string; title:string; source_name:string; content:string; sha256:string; status:string; enabled:boolean; created_at:string; updated_at:string };
type TodoItem = { id:string; title:string; detail:string; status:string; priority:string; due_at?:string; reminder_enabled:boolean; contact_id?:string; source_message_id?:string; kind:string; account_id?:string; admin_contact_id?:string; admin_notification_message_id?:string; response_message_id?:string; delivery_status:string; response_text:string; last_error?:string; completed_at?:string; created_at:string; updated_at:string };
type CalendarTool = { date:string; weekday:string; beijing_time:string; timezone:string; is_today:boolean; upcoming:Array<{id:string;title:string;due_at:string;priority:string;kind:string}> };
type WeatherTool = { location:string; observed_at?:string; current:{weather:string;temperature?:number;apparent_temperature?:number;humidity?:number;precipitation?:number;wind_speed?:number}; forecast:Array<{date:string;weather:string;temperature_max?:number;temperature_min?:number;precipitation_probability?:number}>; source:string };
type SearchTool = { query:string; results:Array<{title:string;url:string;snippet:string}>; source:string };
type DailyDigest = { id:string; local_date:string; status:string; content:string; metrics:Record<string,number>; generated_at:string };
type StickerAsset = { id:string; label:string; tags:string[]; mime_type:string; sha256:string; source_kind:string; source_ref?:string; enabled:boolean; auto_reply_enabled:boolean; use_count:number; created_at:string; updated_at:string };
type StickerCacheCandidate = { id:string; file_name:string; mime_type:string; size_bytes:number; modified_at:string; source_category:string };
type StickerCacheScan = { roots:string[]; candidates:StickerCacheCandidate[] };
type RoutingInfo = { mode:string; providers:Array<{name:string;model:string;priority:number}>; rules:Array<{task:string;detail:string}>; latest?:{task_type?:string;reason?:string;provider_order?:string[];created_at:string}|null };
type Incident = { id:string; kind:string; severity:string; title:string; detail:string; resolved?:boolean; created_at:string };
type Log = { id:string; event:string; level:string; detail:Record<string, unknown>; created_at:string };
type Account = { id:string; platform:string; display_name:string; status:string; enabled:boolean; experimental:boolean; app_id?:string; api_base?:string; managed_qq_id?:string; strategy?:string; risk_acknowledged:boolean; credential_present:boolean; last_error?:string };
type Persona = { global_persona:string; user_style:string; safety_policy:string; updated_at:string };
type QQAcceptance = { id?:string; status:string; target_contact_id?:string; target_name?:string; expected_messages?:number; unique_received?:number; remaining_messages?:number; sent_replies?:number; blocked_or_cancelled?:number; duplicate_webhooks_safely_ignored?:number; duplicate_sends?:number; wrong_recipient_sends?:number; kill_switch_tested?:boolean; kill_switch_violations?:number; connector_or_platform_failures?:number; pending_inbox?:number; system_violations?:number; started_at?:string; completed_at?:string };
type Readiness = { ready:boolean; release_gate:string; checks:Array<{id:string;label:string;ready:boolean;detail:string}>; providers:Array<{id:string;name:string;provider_type:string;enabled:boolean;tested_ok:boolean;tested_at?:string}> };
type RealtimeEvent = { event:string; data?:{ conversation_id?:string } };
type Usage = { today:{messages:number;input_tokens:number;output_tokens:number;total_tokens:number}; last_7_days:Array<{date:string;messages:number;input_tokens:number;output_tokens:number;total_tokens:number}>; by_model:Array<{provider:string;model:string;messages:number;input_tokens:number;output_tokens:number;total_tokens:number}> };
type ExportDefaults = { output_directory:string; start_at:string; end_at:string; format:'PDF'|'TXT'|'MARKDOWN'; include_contact_messages:boolean; include_ai_messages:boolean; include_human_messages:boolean; include_attachments:boolean; max_messages:number };
type ExportResult = { output_directory:string; record_files:string[]; archive_file?:string|null; contact_count:number; message_count:number; attachment_count:number; missing_attachment_count:number; truncated:boolean };
type Diagnostic = {message:{id:string;content:string;author:string;status:string;event_at:string};contact?:{id:string;display_name:string};account?:{id:string;display_name:string};outcome:{code:string;reason:string;replied:boolean};steps:Array<{key:string;label:string;status:string;detail:string}>;outbounds:Array<{id:string;status:string;provider?:string;model?:string;policy_reason?:string;content:string}>;timeline:Array<{event:string;level:string;detail:Record<string,unknown>;created_at:string}>};
type ReferencePreview = {has_reference:boolean;source:string;requested_external_id?:string;message?:{id:string;external_message_id:string;author:string;content:string;message_type:string;created_at:string};attachments:MediaAttachment[]};
type ContactDetail = {contact:Contact;account?:{id:string;display_name:string;enabled:boolean};effective_policy:{reply_time_window_enabled:boolean;reply_auto_start:string;reply_auto_end:string;media_storage_enabled:boolean;media_ai_reply_enabled:boolean};counts:{messages:number;conversations:number;has_media:boolean};recent_messages:Array<{id:string;author:string;content:string;status:string;created_at:string}>;recent_anomalies:Array<{event:string;level:string;detail:Record<string,unknown>;created_at:string}>};
type MediaLibraryItem = MediaAttachment&{message_id:string;message_created_at:string;message_content:string;contact?:{id:string;display_name:string;platform_user_id:string};account?:{id:string;display_name:string}};
type SelfCheck = {status:string;checks:Array<{key:string;label:string;status:string;detail:string;suggestion:string}>};
type ConfigurationReport = {status:string;passed:number;total:number;supervisor:{online:boolean;pid?:number;restart_count?:number;desired_state?:string};items:Array<{id:string;section:string;status:string;title:string;detail:string;suggestion:string;can_retry:boolean}>};
type ModelHealth = {primary_provider?:string;primary_model?:string;actual_provider?:string;actual_model?:string;fallback_active:boolean;last_response_at?:string;pure_local:boolean;providers:Array<{id:string;name:string;model:string;provider_type:string;enabled:boolean;priority:number;is_primary:boolean;is_local:boolean;status:string;consecutive_failures:number;last_success_at?:string;last_failure_at?:string;last_error_code?:string;last_error_detail?:string;last_latency_ms?:number}>};
type OutboxItem = {id:string;contact_name:string;content:string;message_type:string;status:string;provider?:string;model?:string;policy_reason?:string;external_message_id?:string;created_at:string;retry_allowed:boolean;retry_reason:string;attachment_count:number;timeline:Array<{id:string;attempt_no:number;stage:string;status:string;delivery_started:boolean;acknowledged:boolean;error_code?:string;created_at:string}>};
type RelationshipReminder = {id:string;contact_id:string;contact_name:string;kind:string;title:string;detail:string;status:string;due_at:string;is_due:boolean;snoozed_until?:string;created_at:string};
type Tab = 'overview'|'system'|'reliability'|'workspace'|'conversations'|'contacts'|'media'|'exports'|'groups'|'simulator'|'accounts'|'ai'|'memory'|'usage'|'logs'|'settings';
type NavGroupId = 'daily'|'operations'|'data'|'configuration';
type NavItem = {id:Tab;label:string;mark:string};
type NavGroup = {id:NavGroupId;label:string;mark:string;items:NavItem[]};

const navGroups:NavGroup[] = [
  {id:'daily',label:'日常使用',mark:'常',items:[
    {id:'overview',label:'控制台',mark:'控'},
    {id:'conversations',label:'实时会话',mark:'聊'},
    {id:'contacts',label:'联系人',mark:'人'},
    {id:'workspace',label:'智能工作台',mark:'工'},
  ]},
  {id:'operations',label:'运行维护',mark:'运',items:[
    {id:'system',label:'启停与自检',mark:'检'},
    {id:'reliability',label:'可靠性中心',mark:'稳'},
    {id:'logs',label:'日志与风险',mark:'记'},
    {id:'settings',label:'发布门禁',mark:'闸'},
  ]},
  {id:'data',label:'数据与记忆',mark:'数',items:[
    {id:'media',label:'媒体资料库',mark:'媒'},
    {id:'exports',label:'消息导出',mark:'出'},
    {id:'memory',label:'独立记忆',mark:'忆'},
    {id:'usage',label:'用量',mark:'量'},
  ]},
  {id:'configuration',label:'接入与策略',mark:'策',items:[
    {id:'accounts',label:'连接账号',mark:'连'},
    {id:'ai',label:'AI 与人格',mark:'智'},
    {id:'groups',label:'群聊',mark:'群'},
    {id:'simulator',label:'安全演练',mark:'演'},
  ]},
];
const navItems = navGroups.flatMap(group=>group.items);

class ApiError extends Error {
  constructor(message:string, readonly status?:number) { super(message); this.name='ApiError'; }
}

async function api<T>(path:string, token:string, init:RequestInit = {}, timeoutMs=12000):Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(()=>controller.abort(),timeoutMs);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      cache:'no-store',
      signal:init.signal ?? controller.signal,
      headers: { 'Content-Type':'application/json', ...(token ? { Authorization:`Bearer ${token}` } : {}), ...(init.headers ?? {}) },
    });
    if (response.status === 204) return undefined as T;
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(body.detail ?? `请求失败 (${response.status})`,response.status);
    return body as T;
  } catch (reason) {
    if (controller.signal.aborted) throw new ApiError('本机服务响应超时，请检查服务状态后重试');
    throw reason;
  } finally {
    window.clearTimeout(timeout);
  }
}

function apiDate(value:string) {
  // Historical MySQL DATETIME rows may not contain an offset. Neko stores
  // those values as UTC, so make that contract explicit before rendering.
  return new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`);
}

function friendlyTime(value?:string) {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN',{ timeZone:BEIJING_TIME_ZONE,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23' }).format(apiDate(value));
}

function dateInput(value:string) {
  const parts=new Intl.DateTimeFormat('en-US',{timeZone:BEIJING_TIME_ZONE,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(apiDate(value));
  const values=Object.fromEntries(parts.map(part=>[part.type,part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

const exportDefaultNow=new Date();
const DEFAULT_EXPORT_END=dateInput(exportDefaultNow.toISOString());
const DEFAULT_EXPORT_START=dateInput(new Date(exportDefaultNow.getTime()-30*86400000).toISOString());

const relationshipOptions=['朋友','管理员','家人','老师','导师','HR','领导','客户','陌生人'];

function SetupCard({configured,onAuthenticated}:{configured:boolean;onAuthenticated:(token:string)=>void}) {
  const [username,setUsername] = useState('');
  const [password,setPassword] = useState('');
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  async function submit(event:FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const result = await api<{access_token:string}>(configured ? '/auth/login' : '/auth/bootstrap','',{ method:'POST',body:JSON.stringify({username,password}) });
      onAuthenticated(result.access_token);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '操作失败'); }
    finally { setBusy(false); }
  }
  return <main className="auth-shell">
    <section className="auth-brand"><div className="auth-cat">N</div><p className="eyebrow">NEKO AI · LOCAL CONTROL</p><h1>把简单聊天交给 AI，<br/>把发送权留在自己手里。</h1><p>本地单用户后台。默认模拟模式，微信安全门禁关闭，真实发送必须逐级验收。</p></section>
    <section className="auth-card"><div><span className="safe-badge">本机访问</span><h2>{configured ? '登录控制台' : '创建后台管理员'}</h2><p>{configured ? '使用本机管理员账号继续。' : '首次启动只允许创建一个管理员账号。密码至少 10 位。'}</p></div>
      <form onSubmit={submit}><label>用户名<input value={username} onChange={e=>setUsername(e.target.value)} minLength={3} required autoComplete="username"/></label><label>密码<input type="password" value={password} onChange={e=>setPassword(e.target.value)} minLength={10} required autoComplete={configured?'current-password':'new-password'}/></label>{error&&<p className="form-error">{error}</p>}<button className="primary-button" disabled={busy}>{busy?'正在验证…':configured?'安全登录':'创建并进入'}</button></form>
    </section>
  </main>;
}

export default function Home() {
  const [phase,setPhase] = useState<'loading'|'offline'|'setup'|'login'|'app'>('loading');
  const [token,setToken] = useState('');
  const [tab,setTab] = useState<Tab>('overview');
  const [openNavGroup,setOpenNavGroup] = useState<NavGroupId|null>('daily');
  const mobileNavigationRef = useRef<HTMLElement>(null);
  const [dashboard,setDashboard] = useState<Dashboard|null>(null);
  const [contacts,setContacts] = useState<Contact[]>([]);
  const [groups,setGroups] = useState<Group[]>([]);
  const [conversations,setConversations] = useState<Conversation[]>([]);
  const [accounts,setAccounts] = useState<Account[]>([]);
  const [providers,setProviders] = useState<Provider[]>([]);
  const [credentials,setCredentials] = useState<Credential[]>([]);
  const [persona,setPersona] = useState<Persona|null>(null);
  const [usage,setUsage] = useState<Usage>({today:{messages:0,input_tokens:0,output_tokens:0,total_tokens:0},last_7_days:[],by_model:[]});
  const [logs,setLogs] = useState<Log[]>([]);
  const [incidents,setIncidents] = useState<Incident[]>([]);
  const [qqAcceptance,setQqAcceptance] = useState<QQAcceptance>({status:'NOT_STARTED'});
  const [readiness,setReadiness] = useState<Readiness>({ready:false,release_gate:'SIMULATION',checks:[],providers:[]});
  const [mediaUnderstandingStatus,setMediaUnderstandingStatus] = useState<MediaUnderstandingStatus|null>(null);
  const [notice,setNotice] = useState('');
  const [busy,setBusy] = useState(false);
  const [realtimeConnected,setRealtimeConnected] = useState(false);
  const [generatingConversations,setGeneratingConversations] = useState<Set<string>>(()=>new Set());

  const loadAll = useCallback(async (activeToken:string, backgroundSecondary=false) => {
    // The dashboard is the only hard requirement for entering the app. Secondary
    // panels load independently so one slow diagnostic endpoint cannot hold the
    // whole interface on the splash screen forever.
    const dash = await api<Dashboard>('/dashboard',activeToken);
    setDashboard(dash);
    const loadSecondary = async () => {
      const [contactsResult,groupsResult,conversationsResult,accountsResult,providersResult,credentialsResult,personaResult,usageResult,logsResult,incidentsResult,acceptanceResult,readinessResult,mediaStatusResult] = await Promise.allSettled([
        api<Contact[]>('/contacts',activeToken), api<Group[]>('/groups',activeToken),
        api<Conversation[]>('/conversations',activeToken), api<Account[]>('/accounts',activeToken), api<Provider[]>('/providers',activeToken),
        api<Credential[]>('/credentials',activeToken), api<Persona>('/persona',activeToken), api<Usage>('/usage',activeToken), api<Log[]>('/logs?limit=200',activeToken),
        api<Incident[]>('/incidents?resolved=false',activeToken),
        api<QQAcceptance>('/acceptance/qq/current',activeToken),
        api<Readiness>('/acceptance/readiness',activeToken),
        api<MediaUnderstandingStatus>('/media-understanding/status',activeToken),
      ]);
      if(contactsResult.status==='fulfilled')setContacts(contactsResult.value);
      if(groupsResult.status==='fulfilled')setGroups(groupsResult.value);
      if(conversationsResult.status==='fulfilled')setConversations(conversationsResult.value);
      if(accountsResult.status==='fulfilled')setAccounts(accountsResult.value);
      if(providersResult.status==='fulfilled')setProviders(providersResult.value);
      if(credentialsResult.status==='fulfilled')setCredentials(credentialsResult.value);
      if(personaResult.status==='fulfilled')setPersona(personaResult.value);
      if(usageResult.status==='fulfilled')setUsage(usageResult.value);
      if(logsResult.status==='fulfilled')setLogs(logsResult.value);
      if(incidentsResult.status==='fulfilled')setIncidents(incidentsResult.value);
      if(acceptanceResult.status==='fulfilled')setQqAcceptance(acceptanceResult.value);
      if(readinessResult.status==='fulfilled')setReadiness(readinessResult.value);
      if(mediaStatusResult.status==='fulfilled')setMediaUnderstandingStatus(mediaStatusResult.value);
      const failed=[contactsResult,groupsResult,conversationsResult,accountsResult,providersResult,credentialsResult,personaResult,usageResult,logsResult,incidentsResult,acceptanceResult,readinessResult,mediaStatusResult].filter(result=>result.status==='rejected').length;
      if(failed)setNotice(`${failed} 个次要面板暂未加载，可进入后台后重试刷新`);
    };
    if(backgroundSecondary){void loadSecondary();return;}
    await loadSecondary();
  },[]);

  const boot = useCallback(async () => {
    setPhase('loading');
    try {
      const status = await api<{configured:boolean}>('/auth/bootstrap-status','');
      const stored = window.localStorage.getItem('neko_token') ?? '';
      if (!status.configured) { setPhase('setup'); return; }
      if (!stored) { setPhase('login'); return; }
      try { await loadAll(stored,true); setToken(stored); setPhase('app'); }
      catch (reason) {
        if(reason instanceof ApiError&&(reason.status===401||reason.status===403)){window.localStorage.removeItem('neko_token');setPhase('login');}
        else {setNotice(reason instanceof Error?reason.message:'本机服务暂时不可用');setPhase('offline');}
      }
    } catch { setPhase('offline'); }
  },[loadAll]);

  useEffect(()=>{ const timer=window.setTimeout(()=>void boot(),0); return()=>window.clearTimeout(timer); },[boot]);
  useEffect(()=>{
    if (phase!=='app'||!token) return;
    let disposed=false, socket:WebSocket|null=null, pingTimer:number|undefined, reconnectTimer:number|undefined, attempt=0;
    const clearPing=()=>{if(pingTimer!==undefined){window.clearInterval(pingTimer);pingTimer=undefined}};
    const connect=()=>{
      if(disposed)return;
      socket=new WebSocket(`${WS_URL}?token=${encodeURIComponent(token)}`);
      socket.onopen=()=>{attempt=0;setRealtimeConnected(true);clearPing();pingTimer=window.setInterval(()=>{if(socket?.readyState===WebSocket.OPEN)socket.send('ping')},25000)};
      socket.onmessage=message=>{
        let payload:RealtimeEvent|undefined;
        try{payload=JSON.parse(String(message.data)) as RealtimeEvent}catch{/* Unknown events still trigger a safe refresh. */}
        if(payload?.event==='pong')return;
        const conversationId=payload?.data?.conversation_id;
        if(payload?.event==='ai_started'&&conversationId)setGeneratingConversations(current=>new Set(current).add(conversationId));
        if(payload?.event==='ai_finished'&&conversationId)setGeneratingConversations(current=>{const next=new Set(current);next.delete(conversationId);return next});
        void loadAll(token).catch(reason=>setNotice(reason instanceof Error?reason.message:'刷新失败'));
      };
      socket.onerror=()=>socket?.close();
      socket.onclose=()=>{clearPing();setRealtimeConnected(false);setGeneratingConversations(new Set());if(!disposed){attempt=Math.min(attempt+1,4);reconnectTimer=window.setTimeout(connect,Math.min(1000*(2**attempt),15000))}};
    };
    connect();
    return ()=>{disposed=true;clearPing();if(reconnectTimer!==undefined)window.clearTimeout(reconnectTimer);socket?.close();setRealtimeConnected(false);setGeneratingConversations(new Set())};
  },[phase,token,loadAll]);
  useEffect(()=>{ if(!notice)return; const timer=window.setTimeout(()=>setNotice(''),3600); return()=>window.clearTimeout(timer); },[notice]);
  useEffect(()=>{
    mobileNavigationRef.current?.querySelector<HTMLElement>('[aria-current="page"]')?.scrollIntoView({block:'nearest',inline:'center'});
  },[tab]);

  function authenticated(value:string) {
    window.localStorage.setItem('neko_token',value); setToken(value);
    void loadAll(value,true).then(()=>setPhase('app')).catch(reason=>{setNotice(reason instanceof Error?reason.message:'本机服务暂时不可用');setPhase('offline')});
  }
  function signOut() { window.localStorage.removeItem('neko_token'); setToken(''); setPhase('login'); }
  function selectNavigationTab(value:Tab) { const group=navGroups.find(item=>item.items.some(child=>child.id===value)); if(group)setOpenNavGroup(group.id); setTab(value); }
  async function run<T>(action:()=>Promise<T>,success:string) { setBusy(true); try { await action(); await loadAll(token); setNotice(success); } catch(reason) { setNotice(reason instanceof Error?reason.message:'操作失败'); } finally { setBusy(false); } }
  async function setMode(mode:string) { await run(()=>api('/runtime/mode',token,{method:'PUT',body:JSON.stringify({mode})}),`已切换为 ${mode}`); }
  async function toggleKill() {
    const enabled = !dashboard?.runtime.kill_switch;
    if(enabled && !window.confirm('确认立即停止所有自动回复，并取消队列中的待发消息？')) return;
    await run(()=>api('/runtime/kill-switch',token,{method:'PUT',body:JSON.stringify({enabled})}),enabled?'全局急停已开启':'急停已解除，仍需确认运行模式');
  }
  async function updateLiveTimeWindow(enabled:boolean,start:string,end:string) {
    await run(
      ()=>api('/runtime/live-time-window',token,{method:'PUT',body:JSON.stringify({enabled,start,end,disable_confirmed:!enabled})}),
      enabled?`LIVE 时间门禁已保存：${start}–${end}`:'LIVE 时间门禁已关闭；其他安全门禁仍然有效',
    );
  }
  async function updateMediaPolicy(policy:MediaPolicyInput) {
    await run(
      ()=>api('/runtime/media-policy',token,{method:'PUT',body:JSON.stringify(policy)}),
      policy.storage_enabled?'媒体保存策略已更新':'媒体文件落盘已关闭；消息元数据仍会保留',
    );
  }

  if (phase==='loading') return <main className="state-screen"><div className="loader-dot"/><h1>Neko AI 正在检查本机状态</h1><p>核心状态就绪后会立即进入，其余面板将在后台加载。</p><form action="/" method="get"><button className="primary-button compact" name="recover" value="1">重新载入后台</button></form><noscript><p className="form-error">浏览器没有运行 JavaScript，后台无法启动。请启用后重新打开页面。</p></noscript></main>;
  if (phase==='offline') return <main className="state-screen"><span className="offline-mark">!</span><h1>后端服务尚未连接</h1><p>请先启动 Neko AI 本地服务，再重新连接。不会尝试任何真实发送。</p><button className="primary-button compact" onClick={()=>void boot()}>重新连接</button></main>;
  if (phase==='setup'||phase==='login') return <SetupCard configured={phase==='login'} onAuthenticated={authenticated}/>;

  const runtime = dashboard?.runtime;
  return <main className="app-shell wide-shell">
    <aside className="sidebar wide-sidebar">
      <div className="brand-row"><div className="brand-mark">N</div><div><strong>NEKO AI</strong><span>本地控制室</span></div></div>
      <nav className="sidebar-navigation desktop-sidebar-navigation" aria-label="分组主导航">
        {navGroups.map(group=>{
          const expanded=openNavGroup===group.id;
          const current=group.items.some(item=>item.id===tab);
          return <section className={`nav-group ${expanded?'expanded':''} ${current?'current':''}`} key={group.id}>
            <button type="button" className="nav-group-trigger" aria-expanded={expanded} aria-controls={`nav-group-${group.id}`} onClick={()=>setOpenNavGroup(value=>value===group.id?null:group.id)}>
              <span className="nav-group-mark">{group.mark}</span><strong>{group.label}</strong><span className="nav-group-count">{group.items.length}</span>
            </button>
            {expanded&&<div className="nav-group-items" id={`nav-group-${group.id}`}>
              {group.items.map(item=><button type="button" key={item.id} aria-current={tab===item.id?'page':undefined} className={`nav-item wide-nav ${tab===item.id?'active':''}`} onClick={()=>selectNavigationTab(item.id)}><span>{item.mark}</span>{item.label}</button>)}
            </div>}
          </section>;
        })}
      </nav>
      <nav ref={mobileNavigationRef} className="sidebar-navigation mobile-sidebar-navigation" aria-label="移动端主导航">
        {navItems.map(item=><button type="button" key={item.id} aria-current={tab===item.id?'page':undefined} className={`nav-item wide-nav ${tab===item.id?'active':''}`} onClick={()=>selectNavigationTab(item.id)}><span>{item.mark}</span>{item.label}</button>)}
      </nav>
      <button className={`sidebar-kill ${runtime?.kill_switch?'active':''}`} onClick={()=>void toggleKill()} disabled={busy}><span>{runtime?.kill_switch?'已急停':'紧急停止'}</span><small>{runtime?.kill_switch?'所有发送已锁定':'停止所有自动回复'}</small></button>
      <button className="signout" onClick={signOut}>退出本机后台</button>
    </aside>
    <section className="workspace wide-workspace">
      <header className="topbar sticky-top"><div><p className="eyebrow">{navItems.find(item=>item.id===tab)?.label.toUpperCase()}</p><h1>{navItems.find(item=>item.id===tab)?.label}</h1></div><div className="top-actions"><span className={`realtime-pill ${realtimeConnected?'connected':''}`}>{realtimeConnected?'实时已连接':'实时重连中'}</span><span className={`gate-pill ${runtime?.release_gate.toLowerCase()}`}><span className="pulse"/>{runtime?.release_gate}</span><button className="ghost-button" onClick={()=>void loadAll(token)}>刷新</button></div></header>
      {notice&&<div role="status" className="toast">{notice}</div>}
      {tab==='overview'&&dashboard&&<Overview dashboard={dashboard} mediaStatus={mediaUnderstandingStatus} busy={busy} setMode={setMode} toggleKill={toggleKill} updateLiveTimeWindow={updateLiveTimeWindow} updateMediaPolicy={updateMediaPolicy}/>} 
      {tab==='system'&&<SystemCenter token={token} notify={setNotice}/>} 
      {tab==='reliability'&&<ReliabilityCenter token={token} notify={setNotice}/>}
      {tab==='workspace'&&<WorkspacePanel contacts={contacts} token={token} notify={setNotice}/>}
      {tab==='contacts'&&<ContactsPanel contacts={contacts} accounts={accounts} token={token} refresh={()=>loadAll(token)} notify={setNotice}/>} 
      {tab==='media'&&<MediaLibraryPanel contacts={contacts} accounts={accounts} token={token} notify={setNotice}/>} 
      {tab==='exports'&&<ChatExportPanel contacts={contacts} token={token} notify={setNotice}/>} 
      {tab==='groups'&&<GroupsPanel groups={groups} token={token} refresh={()=>loadAll(token)} notify={setNotice}/>} 
      {tab==='conversations'&&<ConversationsPanel conversations={conversations} generating={generatingConversations} token={token} refresh={()=>loadAll(token)} notify={setNotice}/>} 
      {tab==='simulator'&&<SimulatorPanel contacts={contacts} token={token} refresh={()=>loadAll(token)} notify={setNotice}/>} 
      {tab==='accounts'&&<AccountsPanel accounts={accounts} token={token} refresh={()=>loadAll(token)} notify={setNotice}/>} 
      {tab==='ai'&&<AiPanel providers={providers} credentials={credentials} persona={persona} token={token} refresh={()=>loadAll(token)} notify={setNotice}/>} 
      {tab==='memory'&&<MemoryPanel contacts={contacts} token={token} notify={setNotice}/>} 
      {tab==='usage'&&<UsagePanel usage={usage}/>} 
      {tab==='logs'&&<LogsPanel logs={logs} incidents={incidents} token={token} refresh={()=>loadAll(token)} notify={setNotice}/>} 
      {tab==='settings'&&runtime&&<SettingsPanel runtime={runtime} contacts={contacts} acceptance={qqAcceptance} readiness={readiness} token={token} refresh={()=>loadAll(token)} notify={setNotice}/>} 
    </section>
  </main>;
}

function SystemCenter({token,notify}:{token:string;notify:(s:string)=>void}) {
  const [report,setReport]=useState<SelfCheck|null>(null); const [busy,setBusy]=useState(false);
  const inspect=useCallback(async()=>{setBusy(true);try{setReport(await api<SelfCheck>('/system/self-check',token));notify('全面自检已完成')}catch(e){notify(e instanceof Error?e.message:'自检失败')}finally{setBusy(false)}},[notify,token]);
  useEffect(()=>{const timer=window.setTimeout(()=>void inspect(),0);return()=>window.clearTimeout(timer)},[inspect]);
  async function control(action:'start'|'stop'|'restart'){
    const destructive=action!=='start';
    if(destructive&&!window.confirm(action==='stop'?'确认完全停止 Neko 前后端？页面随后会暂时离线。':'确认重启整套 Neko 服务？页面会短暂断开并自动恢复。'))return;
    setBusy(true);
    try{const result=await api<{message:string}>('/system/control',token,{method:'POST',body:JSON.stringify({action,confirmed:destructive})});notify(result.message);if(action==='restart')window.setTimeout(()=>window.location.reload(),6500);if(action==='start')window.setTimeout(()=>void inspect(),3200)}catch(e){notify(e instanceof Error?e.message:'操作失败')}finally{setBusy(false)}
  }
  return <div className="page-stack"><section className="panel system-control"><div className="panel-heading"><div><p className="eyebrow">LOCAL SERVICE CONTROL</p><h3>一键启停与自检中心</h3></div><span className={`health-summary ${report?.status.toLowerCase()}`}>{report?.status??'CHECKING'}</span></div><p className="panel-intro">启动按钮用于确认核心在线并重新自检；停止和重启只操作 Neko 自己记录的进程，不会关闭 QQ、NapCat、Ollama 或 MySQL。</p><div className="system-actions"><button className="secondary-button" disabled={busy} onClick={()=>void control('start')}>启动 / 恢复</button><button className="primary-button compact" disabled={busy} onClick={()=>void control('restart')}>重启 Neko</button><button className="ghost-button danger" disabled={busy} onClick={()=>void control('stop')}>完全停止</button><button className="ghost-button" disabled={busy} onClick={()=>void inspect()}>{busy?'检查中…':'全面自检'}</button></div></section><section className="health-grid">{report?.checks.map(item=><article key={item.key} className={`health-card ${item.status.toLowerCase()}`}><header><span>{item.status==='OK'?'✓':item.status==='WARNING'?'!':'×'}</span><div><p className="eyebrow">{item.key}</p><h3>{item.label}</h3></div><b>{item.status}</b></header><p>{item.detail}</p>{item.suggestion&&<aside><strong>建议</strong>{item.suggestion}</aside>}</article>)}</section>{!report&&<EmptyState title="正在检查本机组件" detail="通常几秒内完成，不会发送任何聊天消息。"/>}</div>;
}

function ReliabilityCenter({token,notify}:{token:string;notify:(s:string)=>void}) {
  const [configuration,setConfiguration]=useState<ConfigurationReport|null>(null);
  const [models,setModels]=useState<ModelHealth|null>(null);
  const [outbox,setOutbox]=useState<OutboxItem[]>([]);
  const [reminders,setReminders]=useState<RelationshipReminder[]>([]);
  const [busy,setBusy]=useState('');
  const load=useCallback(async()=>{
    setBusy('refresh');
    try{
      const [nextConfiguration,nextModels,nextOutbox,nextReminders]=await Promise.all([
        api<ConfigurationReport>('/reliability/configuration',token),
        api<ModelHealth>('/reliability/models',token),
        api<OutboxItem[]>('/reliability/outbox?limit=100',token),
        api<RelationshipReminder[]>('/reliability/reminders?reminder_status=OPEN&limit=100',token),
      ]);
      setConfiguration(nextConfiguration);setModels(nextModels);setOutbox(nextOutbox);setReminders(nextReminders);
    }catch(error){notify(error instanceof Error?error.message:'可靠性状态读取失败')}finally{setBusy('')}
  },[notify,token]);
  useEffect(()=>{const timer=window.setTimeout(()=>void load(),0);return()=>window.clearTimeout(timer)},[load]);
  async function retryCheck(checkId:string){setBusy(checkId);try{setConfiguration(await api(`/reliability/configuration/${encodeURIComponent(checkId)}/retry`,token,{method:'POST'}));setModels(await api('/reliability/models',token));notify('检查已重试；不会发送聊天消息')}catch(error){notify(error instanceof Error?error.message:'重试失败')}finally{setBusy('')}}
  async function retryModel(providerId:string){setBusy(`model:${providerId}`);try{setModels(await api(`/reliability/models/${providerId}/retry`,token,{method:'POST'}));notify('模型连接测试已完成')}catch(error){notify(error instanceof Error?error.message:'模型测试失败')}finally{setBusy('')}}
  async function retryMessage(item:OutboxItem){if(!item.retry_allowed||!window.confirm(`确认安全重试发给“${item.contact_name}”的这条文本？系统会重新检查账号、白名单、门禁和频率。`))return;setBusy(`outbox:${item.id}`);try{await api(`/reliability/outbox/${item.id}/retry`,token,{method:'POST',body:JSON.stringify({confirmed:true})});notify('安全重试已处理');await load()}catch(error){notify(error instanceof Error?error.message:'重试被拒绝')}finally{setBusy('')}}
  async function scanReminders(){setBusy('reminders');try{const result=await api<{created:number;items:RelationshipReminder[]}>('/reliability/reminders/scan',token,{method:'POST'});setReminders(result.items);notify(result.created?`新增 ${result.created} 条管理员提醒`:'没有新的提醒')}catch(error){notify(error instanceof Error?error.message:'扫描失败')}finally{setBusy('')}}
  async function actReminder(item:RelationshipReminder,action:'DONE'|'DISMISS'|'SNOOZE'){setBusy(`reminder:${item.id}`);try{await api(`/reliability/reminders/${item.id}/action`,token,{method:'POST',body:JSON.stringify({action,snooze_days:3})});setReminders(current=>current.filter(row=>row.id!==item.id));notify(action==='SNOOZE'?'已稍后 3 天提醒':action==='DONE'?'已标记完成':'已忽略')}catch(error){notify(error instanceof Error?error.message:'操作失败')}finally{setBusy('')}}
  const failedOutbox=outbox.filter(item=>item.status==='FAILED'||item.status==='QUEUED');
  return <div className="page-stack reliability-center">
    <section className="panel reliability-hero"><div><p className="eyebrow">RELIABILITY CONTROL ROOM</p><h3>可靠性与关系助手</h3><p>把配置缺失、模型降级、消息投递和关系提醒放在一个页面。所有自动提醒仅在本机后台显示。</p></div><div className="reliability-summary"><span className={`health-summary ${configuration?.status.toLowerCase()}`}>{configuration?.passed??0}/{configuration?.total??0} 配置通过</span><span className={models?.pure_local?'safe-badge':'warning-badge'}>{models?.pure_local?'当前纯本地':'包含联网模型'}</span><button className="secondary-button small" disabled={busy==='refresh'} onClick={()=>void load()}>{busy==='refresh'?'刷新中…':'刷新全部'}</button></div></section>
    <section className="panel"><div className="panel-heading"><div><p className="eyebrow">CONFIGURATION WIZARD</p><h3>配置诊断向导</h3></div><span className={configuration?.supervisor.online?'safe-badge':'warning-badge'}>{configuration?.supervisor.online?'同权限监督在线':'尚未安装常驻监督'}</span></div><div className="diagnostic-wizard">{configuration?.items.map(item=><article key={item.id} className={item.status.toLowerCase()}><span>{item.status==='OK'?'✓':item.status==='ERROR'?'×':'!'}</span><div><small>{item.section}</small><strong>{item.title}</strong><p>{item.detail}</p>{item.suggestion&&<em>{item.suggestion}</em>}</div>{item.can_retry&&<button className="secondary-button small" disabled={busy===item.id} onClick={()=>void retryCheck(item.id)}>{busy===item.id?'检查中':'重试'}</button>}</article>)}</div>{!configuration&&<EmptyState title="正在读取配置" detail="会检查本机组件，但不会发送 QQ 消息。"/>}</section>
    <section className="panel"><div className="panel-heading"><div><p className="eyebrow">MODEL DEGRADATION</p><h3>模型降级状态</h3></div><div className="model-current"><span>当前主模型</span><strong>{models?.primary_provider??'未配置'} · {models?.primary_model??'—'}</strong><span>最近实际回复</span><strong>{models?.actual_provider??'尚无'} · {models?.actual_model??'—'}</strong></div></div>{models?.fallback_active&&<div className="degradation-alert">主模型最近没有完成回复，当前实际由兜底模型响应。</div>}<div className="model-health-list">{models?.providers.map(item=><article key={item.id} className={item.status.toLowerCase()}><div><span>{item.is_primary?'主':'第 '+item.priority+' 级'} · {item.provider_type}{item.is_local?' · 本地':''}</span><strong>{item.name}</strong><code>{item.model}</code></div><div><b>{item.enabled?item.status:'DISABLED'}</b><small>最近成功 {friendlyTime(item.last_success_at)}{item.last_latency_ms?` · ${item.last_latency_ms}ms`:''}</small>{item.last_error_detail&&<p>{item.last_error_code} · {item.last_error_detail}</p>}</div>{item.enabled&&<button className="secondary-button small" disabled={busy===`model:${item.id}`} onClick={()=>void retryModel(item.id)}>一键重试</button>}</article>)}</div></section>
    <section className="panel"><div className="panel-heading"><div><p className="eyebrow">DELIVERY LEDGER</p><h3>待发箱与失败补偿</h3></div><span className={failedOutbox.length?'warning-badge':'safe-badge'}>{failedOutbox.length} 条待处理</span></div><p className="hint">只有能够证明“尚未调用发送通道”的纯文本才允许重试；一旦发送开始但结果不明，系统会禁止重试以避免重复发送。</p><div className="outbox-list">{outbox.map(item=><article key={item.id}><header><div><strong>{item.contact_name}</strong><span>{friendlyTime(item.created_at)} · {item.message_type}</span></div><b className={`delivery-state ${item.status.toLowerCase()}`}>{item.status}</b></header><p>{item.content}</p><div className="delivery-timeline">{item.timeline.map(event=><span key={event.id} className={event.acknowledged?'confirmed':event.delivery_started?'started':''}><i/>{event.stage}<small>第 {event.attempt_no} 次 · {event.status}</small></span>)}</div><footer><small>{item.retry_reason}</small>{item.retry_allowed&&<button className="primary-button compact" disabled={busy===`outbox:${item.id}`} onClick={()=>void retryMessage(item)}>安全重试</button>}</footer></article>)}</div>{!outbox.length&&<EmptyState title="待发箱为空" detail="新的 AI 回复会记录生成、排队、发送与确认全过程。"/>}</section>
    <section className="panel"><div className="panel-heading"><div><p className="eyebrow">RELATIONSHIP ASSISTANT</p><h3>智能提醒与关系助手</h3></div><button className="secondary-button small" disabled={busy==='reminders'} onClick={()=>void scanReminders()}>立即扫描</button></div><div className="privacy-banner">仅提醒管理员 · 不会擅自向任何联系人发送消息</div><div className="reminder-list">{reminders.map(item=><article key={item.id} className={item.is_due?'due':''}><span>{item.kind}</span><div><strong>{item.title}</strong><p>{item.detail}</p><small>{item.contact_name} · 提醒时间 {friendlyTime(item.due_at)}</small></div><div className="inline-actions"><button disabled={busy===`reminder:${item.id}`} onClick={()=>void actReminder(item,'DONE')}>完成</button><button disabled={busy===`reminder:${item.id}`} onClick={()=>void actReminder(item,'SNOOZE')}>稍后 3 天</button><button disabled={busy===`reminder:${item.id}`} onClick={()=>void actReminder(item,'DISMISS')}>忽略</button></div></article>)}</div>{!reminders.length&&<EmptyState title="目前没有待处理提醒" detail="生日、长期未回复、待处理承诺和重要话题跟进会出现在这里。"/>}</section>
  </div>;
}

function MediaLibraryPanel({contacts,accounts,token,notify}:{contacts:Contact[];accounts:Account[];token:string;notify:(s:string)=>void}) {
  const [items,setItems]=useState<MediaLibraryItem[]>([]); const [busy,setBusy]=useState(false); const [hasMore,setHasMore]=useState(false);
  const [filter,setFilter]=useState({account_id:'',contact_id:'',kind:'',analysis_status:'',q:''});
  const loadingRef=useRef(false); const mediaRequestRef=useRef(0);
  const load=useCallback(async(offset=0,append=false)=>{if(append&&loadingRef.current)return;const request=++mediaRequestRef.current;loadingRef.current=true;setBusy(true);try{const params=new URLSearchParams({limit:'25',offset:String(offset)});Object.entries(filter).forEach(([key,value])=>{if(value)params.set(key,value)});const response=await api<MediaLibraryItem[]>(`/media-library?${params.toString()}`,token);if(request!==mediaRequestRef.current)return;const page=response.slice(0,24);setHasMore(response.length>24);setItems(current=>append?[...current,...page.filter(item=>!current.some(existing=>existing.id===item.id))]:page)}catch(e){if(request===mediaRequestRef.current)notify(e instanceof Error?e.message:'媒体资料读取失败')}finally{if(request===mediaRequestRef.current){loadingRef.current=false;setBusy(false)}}},[filter,notify,token]);
  useEffect(()=>{const timer=window.setTimeout(()=>void load(0,false),0);return()=>window.clearTimeout(timer)},[load]);
  function onMediaScroll(event:UIEvent<HTMLElement>){const node=event.currentTarget;if(hasMore&&!busy&&node.scrollHeight-node.scrollTop-node.clientHeight<220)void load(items.length,true)}
  const scopedContacts=filter.account_id?contacts.filter(item=>item.account_id===filter.account_id):contacts;
  return <div className="page-stack"><section className="panel"><div className="panel-heading"><div><p className="eyebrow">LOCAL MEDIA ARCHIVE</p><h3>媒体资料库</h3></div><span className="hint">独立滚动并按需加载；只展示 Neko 已记录的附件</span></div><div className="media-filters"><label>托管账号<select value={filter.account_id} onChange={e=>setFilter({...filter,account_id:e.target.value,contact_id:''})}><option value="">全部账号</option>{accounts.map(item=><option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label><label>联系人<select value={filter.contact_id} onChange={e=>setFilter({...filter,contact_id:e.target.value})}><option value="">全部联系人</option>{scopedContacts.map(item=><option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label><label>类型<select value={filter.kind} onChange={e=>setFilter({...filter,kind:e.target.value})}><option value="">全部类型</option><option>IMAGE</option><option>AUDIO</option><option>VIDEO</option><option>FILE</option></select></label><label>识别状态<select value={filter.analysis_status} onChange={e=>setFilter({...filter,analysis_status:e.target.value})}><option value="">全部状态</option><option>COMPLETED</option><option>FAILED</option><option>NOT_REQUESTED</option></select></label><label>文件名 / 识别内容<input value={filter.q} onChange={e=>setFilter({...filter,q:e.target.value})} placeholder="搜索本地记录"/></label><button className="primary-button compact" onClick={()=>void load(0,false)} disabled={busy}>筛选</button></div></section>{items.length?<section className="media-library-scroll" onScroll={onMediaScroll}><div className="media-library-grid">{items.map(item=><article className="panel media-library-card" key={item.id}><header><div><p className="eyebrow">{item.kind} · {friendlyTime(item.message_created_at)}</p><h3>{item.file_name}</h3></div><span className="safe-badge">{item.status}</span></header><p className="media-source">{item.account?.display_name??'未绑定账号'} · {item.contact?.display_name??'未知联系人'}（{item.contact?.platform_user_id??'—'}）</p><MediaAttachmentCard item={item} token={token}/><dl><div><dt>SHA-256</dt><dd><code>{item.sha256??'未生成'}</code></dd></div><div><dt>本地路径</dt><dd><code>{item.local_path??'仅元数据'}</code></dd></div><div><dt>原消息</dt><dd>{item.message_content}</dd></div></dl></article>)}</div>{hasMore&&<button className="secondary-button lazy-load-button" disabled={busy} onClick={()=>void load(items.length,true)}>{busy?'读取中…':'继续加载较早媒体'}</button>}</section>:<EmptyState title={busy?'正在读取媒体资料':'没有符合条件的媒体'} detail="修改筛选条件，或确认联系人已开启媒体保存。"/>}</div>;
}

function Overview({dashboard,mediaStatus,busy,setMode,toggleKill,updateLiveTimeWindow,updateMediaPolicy}:{dashboard:Dashboard;mediaStatus:MediaUnderstandingStatus|null;busy:boolean;setMode:(mode:string)=>Promise<void>;toggleKill:()=>Promise<void>;updateLiveTimeWindow:(enabled:boolean,start:string,end:string)=>Promise<void>;updateMediaPolicy:(policy:MediaPolicyInput)=>Promise<void>}) {
  const runtime=dashboard.runtime, metrics=dashboard.metrics;
  return <div className="page-stack">
    <section className={`safety-hero ${runtime.kill_switch?'stopped':''}`}><div className="hero-copy"><span className="mode-chip">当前模式 · {runtime.global_mode}</span><h2>{runtime.kill_switch?'所有自动发送已锁定。':'自动托管已就绪，'}<br/>{runtime.kill_switch?'等待你解除急停。':'发送权仍由你掌控。'}</h2><p>{runtime.release_gate==='SIMULATION'?'当前为安全演练，消息只进入模拟通道。':'每次发送仍会经过白名单、时段开关、接管、内容与频率复检。'}</p></div><div className="hero-actions"><div className="segmented">{['AUTO','SILENT','READ_ONLY','STOPPED'].map(mode=><button key={mode} className={runtime.global_mode===mode?'selected':''} disabled={busy} onClick={()=>void setMode(mode)}>{mode}</button>)}</div><button className="kill-button" onClick={()=>void toggleKill()} disabled={busy}><span>{runtime.kill_switch?'解除全局急停':'停止所有自动回复'}</span><small>{runtime.kill_switch?'解除后仍受当前模式约束':'取消队列中的全部待发消息'}</small></button></div></section>
    <LiveTimeWindowPanel key={`${runtime.live_time_window_enabled}-${runtime.live_auto_start}-${runtime.live_auto_end}`} runtime={runtime} busy={busy} save={updateLiveTimeWindow}/>
    <MediaPolicyPanel key={`${runtime.updated_at}`} runtime={runtime} status={mediaStatus} busy={busy} save={updateMediaPolicy}/>
    <section><div className="section-heading"><div><p className="eyebrow">TODAY AT A GLANCE</p><h3>今日运行（北京时间）</h3></div><span className="updated">最后更新 {friendlyTime(runtime.updated_at)} · 北京时间</span></div><div className="metrics-grid"><article><p>收到消息</p><strong>{metrics.received??0}</strong><span>本地记录</span></article><article><p>AI 回复</p><strong>{metrics.ai_replies??0}</strong><span>成功发送</span></article><article><p>活跃会话</p><strong>{metrics.active_conversations??0}</strong><span>过去 24 小时</span></article><article className="attention"><p>需要关注</p><strong>{metrics.need_attention??0}</strong><span>未解决风险事件</span></article><article><p>Token 用量</p><strong>{metrics.tokens??0}</strong><span>输入 + 输出</span></article></div></section>
    <section><div className="section-heading"><div><p className="eyebrow">SYSTEM PULSE</p><h3>连接状态</h3></div></div><div className="channel-grid">{dashboard.channels.map((channel,index)=><article className="channel-card" key={`${channel.platform}-${index}`}><div className={`status-orb ${['ONLINE','READY','RUNNING','SIMULATOR'].includes(String(channel.status))?'green':channel.status==='BLOCKED'?'amber':''}`}><span/></div><div><h4>{String(channel.platform)}</h4><p>{channel.platform==='AI'?`${String(channel.configured_providers??0)} 个已启用模型`:channel.real_channel?'真实通道':'隔离模拟通道'}</p></div><span className="status-label">{String(channel.status)}</span></article>)}</div></section>
    <section className="two-column"><div className="panel"><div className="panel-heading"><div><p className="eyebrow">SAFETY INVARIANTS</p><h3>发送前的七道硬门槛</h3></div><span className="safe-badge">代码控制</span></div><div className="invariant-list">{[
      {label:'私聊必须在白名单',active:true},
      {label:'群聊必须允许且 @我',active:true},
      {label:runtime.live_time_window_enabled?`LIVE 仅 ${runtime.live_auto_start}–${runtime.live_auto_end}`:'LIVE 时间门禁已关闭',active:runtime.live_time_window_enabled},
      {label:'全局模式必须为 AUTO',active:true},
      {label:'Manual Only 永不自动发送',active:true},
      {label:'Kill Switch 高于一切',active:true},
      {label:'人工接管期间禁止发送',active:true},
    ].map((item,index)=><div key={item.label}><span>INV-{String(index+1).padStart(2,'0')}</span><p>{item.label}</p><b className={item.active?'':'off'}>{item.active?'ON':'OFF'}</b></div>)}</div></div><div className="panel"><div className="panel-heading"><div><p className="eyebrow">NEED ATTENTION</p><h3>最近风险事件</h3></div></div>{dashboard.recent_incidents.length?<div className="incident-list">{dashboard.recent_incidents.map(item=><article key={item.id}><span className="warning-dot"/><div><strong>{item.title}</strong><p>{item.detail}</p><small>{friendlyTime(item.created_at)}</small></div></article>)}</div>:<EmptyState title="今天很安静" detail="当前没有未处理的风险事件。"/>}</div></section>
  </div>;
}

function LiveTimeWindowPanel({runtime,busy,save}:{runtime:Runtime;busy:boolean;save:(enabled:boolean,start:string,end:string)=>Promise<void>}) {
  const [enabled,setEnabled]=useState(runtime.live_time_window_enabled);
  const [start,setStart]=useState(runtime.live_auto_start);
  const [end,setEnd]=useState(runtime.live_auto_end);
  function toggle(value:boolean){
    if(!value&&!window.confirm('关闭后，LIVE 可在全天任意时间发送。白名单、频率限制、内容检查、人工接管和急停仍然有效。确认关闭时间门禁？'))return;
    setEnabled(value);
  }
  async function submit(event:FormEvent){event.preventDefault();await save(enabled,start,end)}
  return <section className={`panel live-window-panel ${enabled?'':'window-off'}`}>
    <div className="panel-heading"><div><p className="eyebrow">LIVE TIME WINDOW</p><h3>LIVE 自动发送时段（北京时间）</h3></div><span className={enabled?'safe-badge':'warning-badge'}>{enabled?`${start}–${end}`:'全天允许'}</span></div>
    <form className="live-window-form" onSubmit={submit}>
      <label className="switch-field"><Switch checked={enabled} label="LIVE 时间门禁" onChange={toggle}/><span>{enabled?'已启用；支持跨午夜时段':'已关闭；仅建议短时受控测试'}</span></label>
      <label>开始时间（北京时间）<input type="time" value={start} onChange={event=>setStart(event.target.value)} required/></label>
      <label>结束时间（北京时间）<input type="time" value={end} onChange={event=>setEnd(event.target.value)} required/></label>
      <button className="primary-button align-end" disabled={busy}>{busy?'正在保存':'保存 LIVE 时段'}</button>
    </form>
    <p className="window-safety-note">所有时间固定按中国标准时间 Asia/Shanghai（UTC+8）计算，不跟随浏览器或 Windows 时区。SHADOW 始终可全天生成影子回复；此设置只影响 LIVE。每次真实发送前都会重新读取开关与时段，修改设置时会取消已有待发消息。</p>
  </section>
}

function MediaPolicyPanel({runtime,status,busy,save}:{runtime:Runtime;status:MediaUnderstandingStatus|null;busy:boolean;save:(policy:MediaPolicyInput)=>Promise<void>}) {
  const [storageEnabled,setStorageEnabled]=useState(runtime.media_storage_enabled);
  const [saveImages,setSaveImages]=useState(runtime.media_save_images);
  const [saveAudio,setSaveAudio]=useState(runtime.media_save_audio);
  const [saveFiles,setSaveFiles]=useState(runtime.media_save_files);
  const [aiReplyEnabled,setAiReplyEnabled]=useState(runtime.media_ai_reply_enabled);
  const [maxFileMb,setMaxFileMb]=useState(runtime.media_max_file_mb);
  const [understandingEnabled,setUnderstandingEnabled]=useState(runtime.media_understanding_enabled);
  const [understandImages,setUnderstandImages]=useState(runtime.media_understand_images);
  const [transcribeAudio,setTranscribeAudio]=useState(runtime.media_transcribe_audio);
  const [extractDocuments,setExtractDocuments]=useState(runtime.media_extract_documents);
  const [visionModel,setVisionModel]=useState(runtime.media_vision_model);
  const [whisperModel,setWhisperModel]=useState(runtime.media_whisper_model);
  const [whisperDevice,setWhisperDevice]=useState(runtime.media_whisper_device);
  const [whisperAllowDownload,setWhisperAllowDownload]=useState(runtime.media_whisper_allow_download);
  const [understandingMaxChars,setUnderstandingMaxChars]=useState(runtime.media_understanding_max_chars);
  const [kimiUploadEnabled,setKimiUploadEnabled]=useState(runtime.kimi_media_upload_enabled);
  const [kimiUploadImages,setKimiUploadImages]=useState(runtime.kimi_media_upload_images);
  const [kimiUploadVideos,setKimiUploadVideos]=useState(runtime.kimi_media_upload_videos);
  const [kimiMaxFileMb,setKimiMaxFileMb]=useState(runtime.kimi_media_max_file_mb);
  const [ttsEnabled,setTtsEnabled]=useState(runtime.tts_enabled);
  const [ttsAudioOnly,setTtsAudioOnly]=useState(runtime.tts_reply_to_audio_only);
  const [ttsVoice,setTtsVoice]=useState(runtime.tts_voice.startsWith('male-youth-')?runtime.tts_voice:'male-youth-cute');
  const [ttsRate,setTtsRate]=useState(runtime.tts_rate);
  const [ttsVolume,setTtsVolume]=useState(runtime.tts_volume);
  const [ttsMaxChars,setTtsMaxChars]=useState(runtime.tts_max_chars);
  const [quoteReply,setQuoteReply]=useState(runtime.napcat_quote_reply_enabled);
  async function submit(event:FormEvent){event.preventDefault();await save({storage_enabled:storageEnabled,save_images:saveImages,save_audio:saveAudio,save_files:saveFiles,ai_reply_enabled:aiReplyEnabled,max_file_mb:maxFileMb,understanding_enabled:understandingEnabled,understand_images:understandImages,transcribe_audio:transcribeAudio,extract_documents:extractDocuments,vision_model:visionModel,whisper_model:whisperModel,whisper_device:whisperDevice,whisper_allow_download:whisperAllowDownload,understanding_max_chars:understandingMaxChars,kimi_upload_enabled:kimiUploadEnabled,kimi_upload_images:kimiUploadImages,kimi_upload_videos:kimiUploadVideos,kimi_max_file_mb:kimiMaxFileMb,tts_enabled:ttsEnabled,tts_reply_to_audio_only:ttsAudioOnly,tts_voice:ttsVoice,tts_rate:ttsRate,tts_volume:ttsVolume,tts_max_chars:ttsMaxChars,napcat_quote_reply_enabled:quoteReply})}
  return <section className={`panel media-policy-panel ${storageEnabled?'':'media-off'}`}>
    <div className="panel-heading"><div><p className="eyebrow">LOCAL MULTIMODAL PIPELINE</p><h3>白名单媒体保存与理解</h3></div><span className={understandingEnabled?'safe-badge':'warning-badge'}>{understandingEnabled?'本机识别已开启':'识别默认关闭'}</span></div>
    <form className="media-policy-form" onSubmit={submit}>
      <label className="switch-field media-master"><Switch checked={storageEnabled} label="本机保存媒体" onChange={setStorageEnabled}/><span>白名单联系人发送的媒体会保存到 Neko 本机数据目录</span></label>
      <label className="media-toggle"><Switch checked={saveImages} label="保存图片" onChange={setSaveImages}/><span><b>图片</b><small>保存原图并在会话中预览</small></span></label>
      <label className="media-toggle"><Switch checked={saveAudio} label="保存语音" onChange={setSaveAudio}/><span><b>语音</b><small>优先转换为可播放格式</small></span></label>
      <label className="media-toggle"><Switch checked={saveFiles} label="保存文件" onChange={setSaveFiles}/><span><b>文件 / 视频</b><small>保存原文件并提供下载</small></span></label>
      <label>单个文件上限（MB）<input type="number" min={1} max={500} value={maxFileMb} onChange={event=>setMaxFileMb(Number(event.target.value))} required/></label>
      <div className="media-understanding-divider"><strong>本机开源模型识别</strong><span>识别内容按联系人消息处理，不能覆盖安全规则</span></div>
      <label className="switch-field media-master"><Switch checked={understandingEnabled} label="启用媒体理解" onChange={setUnderstandingEnabled}/><span>图片使用 Ollama 视觉模型，语音使用 faster-whisper，文档只读提取文字</span></label>
      <label className="media-toggle"><Switch checked={understandImages} label="识别图片" onChange={setUnderstandImages}/><span><b>图片描述 / OCR</b><small>{status?.vision.service_online?(status.vision.model_installed?'模型就绪':'Ollama 在线，模型未安装'):'Ollama 未连接'}</small></span></label>
      <label className="media-toggle"><Switch checked={transcribeAudio} label="识别语音" onChange={setTranscribeAudio}/><span><b>Whisper 转写</b><small>{status?.speech.model_cached?`${status.speech.model} 已缓存`:status?.speech.package_installed?'组件已安装，模型未缓存':'组件未安装'}</small></span></label>
      <label className="media-toggle"><Switch checked={extractDocuments} label="读取文档" onChange={setExtractDocuments}/><span><b>文本 / DOCX / PDF</b><small>{status?.documents.pdf?'全部解析器就绪':'PDF 解析器未安装'}</small></span></label>
      <label>视觉模型<input value={visionModel} maxLength={160} onChange={event=>setVisionModel(event.target.value)} placeholder="qwen3-vl:4b" required/></label>
      <label>Whisper 模型<select value={whisperModel} onChange={event=>setWhisperModel(event.target.value)}><option>tiny</option><option>base</option><option>small</option><option>medium</option><option>large-v3</option><option>turbo</option></select></label>
      <label>Whisper 设备<select value={whisperDevice} onChange={event=>setWhisperDevice(event.target.value)}><option value="auto">自动（优先 GPU）</option><option value="cuda">仅 GPU</option><option value="cpu">仅 CPU</option></select></label>
      <label>单附件最多提取字符<input type="number" min={500} max={20000} step={500} value={understandingMaxChars} onChange={event=>setUnderstandingMaxChars(Number(event.target.value))} required/></label>
      <label className="switch-field model-download-toggle"><Switch checked={whisperAllowDownload} label="允许首次下载 Whisper 模型" onChange={setWhisperAllowDownload}/><span>关闭时只使用已缓存模型；开启后第一次语音可能需要较长时间</span></label>
      <label className="switch-field ai-media-toggle"><Switch checked={aiReplyEnabled} label="识别成功后用于回复" onChange={setAiReplyEnabled}/><span>纯媒体只有识别成功才进入回复模型；失败时保存文件但不猜测内容</span></label>
      <div className="media-understanding-divider"><strong>Kimi Cloud 原始媒体</strong><span>图片/视频会优先尝试 Kimi；纯文字仍按模型优先级</span></div>
      <label className="switch-field media-master"><Switch checked={kimiUploadEnabled} label="允许上传至 Kimi Cloud" onChange={setKimiUploadEnabled}/><span>只处理白名单联系人已安全保存的图片和视频；默认关闭</span></label>
      <label className="media-toggle"><Switch checked={kimiUploadImages} label="上传图片" onChange={setKimiUploadImages}/><span><b>原图理解</b><small>Kimi 可结合文字直接查看图片</small></span></label>
      <label className="media-toggle"><Switch checked={kimiUploadVideos} label="上传视频" onChange={setKimiUploadVideos}/><span><b>视频理解</b><small>Kimi 可结合文字分析视频内容</small></span></label>
      <label>Kimi 单文件上限（MB）<input type="number" min={1} max={100} value={kimiMaxFileMb} onChange={event=>setKimiMaxFileMb(Number(event.target.value))} required/></label>
      <div className="media-understanding-divider"><strong>本地语音与引用回复</strong><span>均只作用于 NapCat；开关默认关闭，发送前仍经过原有 LIVE 门禁</span></div>
      <label className="switch-field media-master"><Switch checked={ttsEnabled} label="本地 TTS 语音回复" onChange={setTtsEnabled}/><span>{status?.tts.supported?status.tts.detail:'当前系统不支持本机 TTS'}</span></label>
      <label className="switch-field"><Switch checked={ttsAudioOnly} label="只对语音消息回语音" onChange={setTtsAudioOnly}/><span>建议开启，避免每条文字回复都附带语音</span></label>
      <label>橙蓝本地少年音<select value={ttsVoice} onChange={event=>setTtsVoice(event.target.value)}><option value="male-youth-cute">软萌可爱少年（推荐）</option><option value="male-youth-bright">元气活泼少年</option><option value="male-youth-soft">温柔乖巧少年</option></select></label>
      <label>语速<input type="number" min={-3} max={4} value={ttsRate} onChange={event=>setTtsRate(Number(event.target.value))}/></label>
      <label>音量<input type="number" min={1} max={100} value={ttsVolume} onChange={event=>setTtsVolume(Number(event.target.value))}/></label>
      <label>单条语音最多字符<input type="number" min={30} max={1000} value={ttsMaxChars} onChange={event=>setTtsMaxChars(Number(event.target.value))}/></label>
      <label className="switch-field media-master"><Switch checked={quoteReply} label="主动引用对应消息" onChange={setQuoteReply}/><span>回复时引用触发本轮的原消息；引用失效会自动退回普通回复</span></label>
      <button className="primary-button" disabled={busy}>{busy?'正在保存':'保存媒体策略'}</button>
    </form>
    <p className="window-safety-note">本机识别状态：视觉 {status?.vision.service_online?(status.vision.model_installed?`${status.vision.model} 已安装`:`需先安装 ${visionModel}`):'Ollama 未连接'}；语音 {status?.speech.model_cached?`${status.speech.model} 模型已缓存`:status?.speech.package_installed?'faster-whisper 已安装，权重未缓存':'需安装后端语音组件'}。Kimi 上传开启后，图片/视频优先交给已启用的 Kimi；云端临时文件在回答完成或失败后会请求删除，普通文件不会上传或执行。</p>
  </section>
}

function ChatExportPanel({contacts,token,notify}:{contacts:Contact[];token:string;notify:(s:string)=>void}) {
  const [selected,setSelected]=useState<string[]>([]);
  const [startDate,setStartDate]=useState(DEFAULT_EXPORT_START);
  const [endDate,setEndDate]=useState(DEFAULT_EXPORT_END);
  const [format,setFormat]=useState<'PDF'|'TXT'|'MARKDOWN'>('MARKDOWN');
  const [includeContact,setIncludeContact]=useState(true);
  const [includeAi,setIncludeAi]=useState(true);
  const [includeHuman,setIncludeHuman]=useState(true);
  const [includeAttachments,setIncludeAttachments]=useState(true);
  const [outputDirectory,setOutputDirectory]=useState('');
  const [maxMessages,setMaxMessages]=useState(5000);
  const [busy,setBusy]=useState(false);
  const [result,setResult]=useState<ExportResult|null>(null);
  useEffect(()=>{let active=true;void api<ExportDefaults>('/exports/defaults',token).then(value=>{if(!active)return;setStartDate(dateInput(value.start_at));setEndDate(dateInput(value.end_at));setFormat(value.format);setIncludeContact(value.include_contact_messages);setIncludeAi(value.include_ai_messages);setIncludeHuman(value.include_human_messages);setIncludeAttachments(value.include_attachments);setOutputDirectory(value.output_directory);setMaxMessages(value.max_messages)}).catch(reason=>notify(reason instanceof Error?reason.message:'无法读取导出默认值'));return()=>{active=false}},[token,notify]);
  function toggleContact(id:string){setSelected(current=>current.includes(id)?current.filter(item=>item!==id):[...current,id])}
  async function submit(event:FormEvent){event.preventDefault();if(!selected.length){notify('请至少选择一位联系人');return}if(!includeContact&&!includeAi&&!includeHuman){notify('请至少选择一种消息来源');return}setBusy(true);setResult(null);try{const next=await api<ExportResult>('/exports/chats',token,{method:'POST',body:JSON.stringify({contact_ids:selected,start_at:`${startDate}T00:00:00+08:00`,end_at:`${endDate}T23:59:59.999+08:00`,include_contact_messages:includeContact,include_ai_messages:includeAi,include_human_messages:includeHuman,format,include_attachments:includeAttachments,output_directory:outputDirectory.trim()||null,max_messages:maxMessages})});setResult(next);notify(`已导出 ${next.message_count} 条消息`)}catch(reason){notify(reason instanceof Error?reason.message:'导出失败')}finally{setBusy(false)}}
  async function copyPath(value:string){try{await navigator.clipboard.writeText(value);notify('路径已复制')}catch{notify('浏览器未允许复制，请手动选择路径文本')}}
  const allSelected=contacts.length>0&&selected.length===contacts.length;
  return <div className="page-stack">
    <section className="panel export-panel"><div className="panel-heading"><div><p className="eyebrow">LOCAL CHAT ARCHIVE</p><h3>按联系人和日期导出</h3></div><span className="safe-badge">只读取 Neko 本机记录</span></div>
      <form className="export-form" onSubmit={submit}>
        <fieldset className="export-contacts"><legend>1 · 选择联系人（可多选）</legend><div className="selection-toolbar"><button type="button" className="ghost-button" onClick={()=>setSelected(allSelected?[]:contacts.map(item=>item.id))}>{allSelected?'取消全选':'全选'}</button><span>已选择 {selected.length} / {contacts.length}</span></div><div className="export-contact-grid">{contacts.map(item=><label key={item.id} className={selected.includes(item.id)?'selected':''}><input type="checkbox" checked={selected.includes(item.id)} onChange={()=>toggleContact(item.id)}/><span><b>{item.display_name}</b><small>{item.relationship_label} · {item.platform} · {item.platform_user_id}</small></span></label>)}</div></fieldset>
        <fieldset><legend>2 · 日期范围（北京时间）</legend><div className="export-fields two"><label>开始日期<input type="date" value={startDate} max={endDate} onChange={event=>setStartDate(event.target.value)} required/></label><label>结束日期<input type="date" value={endDate} min={startDate} onChange={event=>setEndDate(event.target.value)} required/></label></div></fieldset>
        <fieldset><legend>3 · 消息来源</legend><div className="export-switches"><label><Switch checked={includeContact} label="对方消息" onChange={setIncludeContact}/><span><b>对方消息</b><small>联系人发来的文字和媒体记录</small></span></label><label><Switch checked={includeAi} label="机器人消息" onChange={setIncludeAi}/><span><b>Neko / 机器人</b><small>AI 回复和确定性系统回复</small></span></label><label><Switch checked={includeHuman} label="本人消息" onChange={setIncludeHuman}/><span><b>本人（HUMAN）</b><small>账号本身手动发送的消息</small></span></label></div></fieldset>
        <fieldset><legend>4 · 文件与上限</legend><div className="export-fields"><label>导出格式<select value={format} onChange={event=>setFormat(event.target.value as 'PDF'|'TXT'|'MARKDOWN')}><option value="MARKDOWN">Markdown（推荐）</option><option value="TXT">TXT</option><option value="PDF">PDF</option></select></label><label>消息上限<input type="number" min={1} max={50000} step={100} value={maxMessages} onChange={event=>setMaxMessages(Number(event.target.value))} required/></label><label className="export-attachment-toggle"><Switch checked={includeAttachments} label="附带附件" onChange={setIncludeAttachments}/><span><b>附带并打包已保存附件</b><small>复制图片、语音、视频和普通文件，并生成 ZIP；缺失文件会计数但不会中断。</small></span></label></div></fieldset>
        <fieldset><legend>5 · 导出目录</legend><label>本机文件夹路径<input value={outputDirectory} onChange={event=>setOutputDirectory(event.target.value)} maxLength={1000} placeholder="留空时使用 backend/data/exports"/><small>每次都会在该目录新建带时间戳的文件夹，不覆盖已有文件。浏览器不能读取你的本机目录列表，因此请粘贴路径。</small></label></fieldset>
        <button className="primary-button export-submit" disabled={busy||!selected.length}>{busy?'正在整理记录和附件…':'开始导出'}</button>
      </form>
    </section>
    {result&&<section className="panel export-result"><div className="panel-heading"><div><p className="eyebrow">EXPORT COMPLETE</p><h3>导出完成</h3></div><span className={result.truncated?'warning-badge':'safe-badge'}>{result.truncated?'达到消息上限':'完整范围'}</span></div><div className="export-metrics"><article><span>联系人</span><strong>{result.contact_count}</strong></article><article><span>消息</span><strong>{result.message_count}</strong></article><article><span>已打包附件</span><strong>{result.attachment_count}</strong></article><article><span>附件缺失</span><strong>{result.missing_attachment_count}</strong></article></div><div className="export-path"><span>导出文件夹</span><code>{result.output_directory}</code><button className="secondary-button small" onClick={()=>void copyPath(result.output_directory)}>复制路径</button></div>{result.archive_file&&<div className="export-path"><span>ZIP 压缩包</span><code>{result.archive_file}</code><button className="secondary-button small" onClick={()=>void copyPath(result.archive_file||'')}>复制路径</button></div>}<div className="export-file-list">{result.record_files.map(item=><code key={item}>{item}</code>)}</div></section>}
    <section className="callout"><strong>导出隐私说明</strong><p>导出只读取已写入 Neko 数据库的消息，不读取 QQ 客户端历史。原始 webhook、Token 和凭据不会写入导出文件；附件只复制 Neko 已安全保存的本机文件。</p></section>
  </div>;
}

function ContactsPanel({contacts,accounts,token,refresh,notify}:{contacts:Contact[];accounts:Account[];token:string;refresh:()=>Promise<void>;notify:(s:string)=>void}) {
  const napcatAccounts=accounts.filter(item=>item.platform==='QQ_NAPCAT');
  const [accountFilter,setAccountFilter]=useState(''); const [detailId,setDetailId]=useState('');
  const [form,setForm]=useState({platform:'SIMULATOR',account_id:'',platform_user_id:'',display_name:'',relationship_label:'朋友',whitelisted:false,importance:'NORMAL'});
  const visible=accountFilter?contacts.filter(item=>item.account_id===accountFilter):contacts;
  async function create(event:FormEvent){event.preventDefault();try{await api('/contacts',token,{method:'POST',body:JSON.stringify(form)});setForm({...form,platform_user_id:'',display_name:''});await refresh();notify('联系人已创建，默认仍受安全策略约束');}catch(e){notify(e instanceof Error?e.message:'创建失败')}}
  async function patch(id:string,data:Record<string,unknown>){try{await api(`/contacts/${id}`,token,{method:'PATCH',body:JSON.stringify(data)});await refresh();notify('联系人策略已更新')}catch(e){notify(e instanceof Error?e.message:'更新失败')}}
  const platformAccounts=accounts.filter(item=>item.platform===form.platform);
  return <div className="page-stack"><section className="panel"><div className="panel-heading"><div><p className="eyebrow">ADD CONTACT</p><h3>创建托管联系人</h3></div><span className="hint">联系人会绑定具体托管账号，切换账号不会串用策略或聊天</span></div><form className="form-grid contact-form" onSubmit={create}><label>平台<select value={form.platform} onChange={e=>setForm({...form,platform:e.target.value,account_id:''})}><option>SIMULATOR</option><option value="QQ">QQ 官方</option><option value="QQ_NAPCAT">QQ · NapCat</option><option value="WECHAT_AUTOWX">微信 · AutoWx 草稿</option><option value="WECHAT">微信官方门禁</option></select></label>{form.platform!=='SIMULATOR'&&<label>所属托管账号<select value={form.account_id} onChange={e=>setForm({...form,account_id:e.target.value})}><option value="">使用当前活动账号</option>{platformAccounts.map(item=><option key={item.id} value={item.id}>{item.display_name}{item.enabled?'（当前）':''}</option>)}</select></label>}<label>平台用户 ID<input value={form.platform_user_id} onChange={e=>setForm({...form,platform_user_id:e.target.value})} required/></label><label>显示名称<input value={form.display_name} onChange={e=>setForm({...form,display_name:e.target.value})} required/></label><label>关系<select value={form.relationship_label} onChange={e=>setForm({...form,relationship_label:e.target.value})}>{relationshipOptions.map(item=><option key={item}>{item}</option>)}</select></label><label>重要程度<select value={form.importance} onChange={e=>setForm({...form,importance:e.target.value})}><option>NORMAL</option><option>IMPORTANT</option><option>MANUAL_ONLY</option></select></label><label className="switch-field contact-create-whitelist"><Switch checked={form.whitelisted} label="创建时加入白名单" onChange={value=>setForm({...form,whitelisted:value})}/><span>管理员必须开启</span></label><button className="primary-button align-end">创建联系人</button></form></section><section className="callout admin-guide"><strong>管理员聊天控制</strong><p>“管理员”只允许用于 NapCat 私聊白名单联系人。发送 <code>/neko 帮助</code> 查看菜单；聊天管理员只能管理自己所属的托管账号，不能跨账号检索或发送。</p></section><section className="panel table-panel"><div className="panel-heading"><div><p className="eyebrow">CONTACT POLICY</p><h3>{visible.length} 位联系人</h3></div><label className="compact-filter">账号范围<select value={accountFilter} onChange={e=>{setAccountFilter(e.target.value);setDetailId('')}}><option value="">全部账号</option>{accounts.map(item=><option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label></div>{visible.length?<div className="data-table contact-table"><div className="table-row table-head contact-table-row"><span>联系人</span><span>关系</span><span>重要程度</span><span>白名单</span><span>AI</span><span>记忆</span><span>续火</span><span>资料</span></div>{visible.map(item=><ContactRow key={item.id} item={item} napcatAccounts={napcatAccounts.filter(account=>!item.account_id||account.id===item.account_id)} patch={patch} openDetail={()=>setDetailId(item.id)}/>)}</div>:<EmptyState title="还没有联系人" detail="先创建一个 SIMULATOR 联系人进行安全演练。"/>}</section>{detailId&&<ContactDetailPanel contactId={detailId} token={token} patch={patch} close={()=>setDetailId('')} notify={notify}/>}</div>;
}

function ContactRow({item,napcatAccounts,patch,openDetail}:{item:Contact;napcatAccounts:Account[];patch:(id:string,data:Record<string,unknown>)=>Promise<void>;openDetail:()=>void}) {
  const [editing,setEditing]=useState(false);
  const [draft,setDraft]=useState({display_name:item.display_name,relationship_label:item.relationship_label,style_profile:item.style_profile,custom_prompt:item.custom_prompt,keepalive_time:item.keepalive_time||'20:00',keepalive_account_id:item.keepalive_account_id||''});
  function begin(){setDraft({display_name:item.display_name,relationship_label:item.relationship_label,style_profile:item.style_profile,custom_prompt:item.custom_prompt,keepalive_time:item.keepalive_time||'20:00',keepalive_account_id:item.keepalive_account_id||''});setEditing(true)}
  async function save(event:FormEvent){event.preventDefault();await patch(item.id,draft);setEditing(false)}
  async function toggleKeepalive(value:boolean){const accountId=item.keepalive_account_id||napcatAccounts.find(account=>account.enabled)?.id||napcatAccounts[0]?.id;if(value&&!accountId)return;await patch(item.id,{keepalive_enabled:value,...(value?{keepalive_account_id:accountId}:{})})}
  const keepaliveAllowed=item.platform==='QQ_NAPCAT'&&napcatAccounts.length>0;
  return <>
    <div className="table-row contact-table-row"><span><b>{item.display_name}</b><small>{item.platform} · {item.platform_user_id}</small></span><span>{item.relationship_label}</span><span><select className="inline-select" value={item.importance} onChange={e=>void patch(item.id,{importance:e.target.value})}><option>NORMAL</option><option>IMPORTANT</option><option>MANUAL_ONLY</option></select></span><span><Switch checked={item.whitelisted} label="白名单" onChange={v=>void patch(item.id,{whitelisted:v})}/></span><span><Switch checked={item.ai_enabled} label="AI" onChange={v=>void patch(item.id,{ai_enabled:v})}/></span><span><Switch checked={item.memory_enabled} label="记忆" onChange={v=>void patch(item.id,{memory_enabled:v})}/></span><span className="keepalive-cell"><Switch checked={item.keepalive_enabled} label="续火" disabled={!keepaliveAllowed} onChange={v=>void toggleKeepalive(v)}/><small>{item.keepalive_enabled?`${item.keepalive_time} · ${item.keepalive_last_status}`:keepaliveAllowed?'未开启':'仅 NapCat'}</small></span><span className="record-actions"><button type="button" className="secondary-button small" onClick={openDetail}>详情</button><button type="button" className="ghost-button" onClick={begin}>快速编辑</button></span></div>
    {editing&&<form className="contact-editor" onSubmit={save}><label>显示名称<input value={draft.display_name} onChange={e=>setDraft({...draft,display_name:e.target.value})} required/></label><label>关系标签<select value={draft.relationship_label} onChange={e=>setDraft({...draft,relationship_label:e.target.value})}>{relationshipOptions.map(value=><option key={value}>{value}</option>)}</select></label><label>续火时间（北京时间）<input type="time" value={draft.keepalive_time} onChange={e=>setDraft({...draft,keepalive_time:e.target.value})} disabled={item.platform!=='QQ_NAPCAT'}/></label><label>续火发送账号<select value={draft.keepalive_account_id} onChange={e=>setDraft({...draft,keepalive_account_id:e.target.value})} disabled={item.platform!=='QQ_NAPCAT'}><option value="">请选择 NapCat 账号</option>{napcatAccounts.map(account=><option value={account.id} key={account.id}>{account.display_name} · {account.managed_qq_id||'未填写QQ号'}{account.enabled?'（当前）':''}</option>)}</select></label><label>该联系人的聊天风格<textarea rows={3} value={draft.style_profile} onChange={e=>setDraft({...draft,style_profile:e.target.value})} maxLength={2000} placeholder="例如：简短、少用表情、称呼对方小林"/></label><label>该联系人的独立提示词<textarea rows={3} value={draft.custom_prompt} onChange={e=>setDraft({...draft,custom_prompt:e.target.value})} maxLength={4000} placeholder="只补充个性化信息，安全规则始终优先"/></label><div className="record-actions"><button className="primary-button compact">保存资料</button><button type="button" className="ghost-button" onClick={()=>setEditing(false)}>取消</button></div>{draft.relationship_label==='管理员'&&<small className="keepalive-history">管理员可通过以 /neko 开头的私聊指令控制运行状态；必须保持 NapCat 白名单。</small>}{item.keepalive_last_sent_at&&<small className="keepalive-history">上次续火：{friendlyTime(item.keepalive_last_sent_at)} · {item.keepalive_last_content}</small>}{item.keepalive_last_error&&<small className="keepalive-history error">最近状态：{item.keepalive_last_error}</small>}</form>}
  </>;
}

function ContactDetailPanel({contactId,token,patch,close,notify}:{contactId:string;token:string;patch:(id:string,data:Record<string,unknown>)=>Promise<void>;close:()=>void;notify:(s:string)=>void}) {
  const [detail,setDetail]=useState<ContactDetail|null>(null); const [busy,setBusy]=useState(false);
  const [draft,setDraft]=useState({display_name:'',relationship_label:'朋友',importance:'NORMAL',style_profile:'',custom_prompt:'',reply_time_window_enabled:'inherit',reply_auto_start:'',reply_auto_end:'',media_storage_enabled:'inherit',media_ai_reply_enabled:'inherit',keepalive_time:'20:00',birthday_mmdd:'',relationship_reminders_enabled:true,dormant_reminder_days:14});
  const load=useCallback(async()=>{try{const next=await api<ContactDetail>(`/contacts/${contactId}/detail`,token);setDetail(next);const item=next.contact;setDraft({display_name:item.display_name,relationship_label:item.relationship_label,importance:item.importance,style_profile:item.style_profile,custom_prompt:item.custom_prompt,reply_time_window_enabled:item.reply_time_window_enabled===null||item.reply_time_window_enabled===undefined?'inherit':String(item.reply_time_window_enabled),reply_auto_start:item.reply_auto_start??'',reply_auto_end:item.reply_auto_end??'',media_storage_enabled:item.media_storage_enabled===null||item.media_storage_enabled===undefined?'inherit':String(item.media_storage_enabled),media_ai_reply_enabled:item.media_ai_reply_enabled===null||item.media_ai_reply_enabled===undefined?'inherit':String(item.media_ai_reply_enabled),keepalive_time:item.keepalive_time||'20:00',birthday_mmdd:item.birthday_mmdd??'',relationship_reminders_enabled:item.relationship_reminders_enabled,dormant_reminder_days:item.dormant_reminder_days||14})}catch(e){notify(e instanceof Error?e.message:'联系人详情读取失败')}},[contactId,notify,token]);
  useEffect(()=>{const timer=window.setTimeout(()=>void load(),0);return()=>window.clearTimeout(timer)},[load]);
  async function save(e:FormEvent){e.preventDefault();setBusy(true);const tri=(value:string)=>value==='inherit'?null:value==='true';try{await patch(contactId,{display_name:draft.display_name,relationship_label:draft.relationship_label,importance:draft.importance,style_profile:draft.style_profile,custom_prompt:draft.custom_prompt,reply_time_window_enabled:tri(draft.reply_time_window_enabled),reply_auto_start:draft.reply_auto_start||null,reply_auto_end:draft.reply_auto_end||null,media_storage_enabled:tri(draft.media_storage_enabled),media_ai_reply_enabled:tri(draft.media_ai_reply_enabled),keepalive_time:draft.keepalive_time,birthday_mmdd:draft.birthday_mmdd||null,relationship_reminders_enabled:draft.relationship_reminders_enabled,dormant_reminder_days:draft.dormant_reminder_days});await load()}finally{setBusy(false)}}
  if(!detail)return <section className="panel"><EmptyState title="正在读取联系人详情" detail="策略、最近消息和异常正在汇总。"/></section>;
  const item=detail.contact;
  return <section className="panel contact-detail">
    <div className="panel-heading"><div><p className="eyebrow">CONTACT DETAIL · {detail.account?.display_name??'未绑定账号'}</p><h3>{item.display_name}</h3></div><button className="ghost-button" onClick={close}>关闭详情</button></div>
    <div className="contact-detail-summary"><article><span>白名单</span><strong>{item.whitelisted?'开启':'关闭'}</strong></article><article><span>AI 回复</span><strong>{item.ai_enabled?'开启':'关闭'}</strong></article><article><span>记忆</span><strong>{item.memory_enabled?'开启':'关闭'}</strong></article><article><span>最近记录</span><strong>{detail.counts.messages}</strong></article><article><span>媒体</span><strong>{detail.counts.has_media?'已有':'暂无'}</strong></article></div>
    <form className="contact-detail-form" onSubmit={save}>
      <label>显示名称<input value={draft.display_name} onChange={e=>setDraft({...draft,display_name:e.target.value})}/></label>
      <label>关系<select value={draft.relationship_label} onChange={e=>setDraft({...draft,relationship_label:e.target.value})}>{relationshipOptions.map(value=><option key={value}>{value}</option>)}</select></label>
      <label>重要程度<select value={draft.importance} onChange={e=>setDraft({...draft,importance:e.target.value})}><option>NORMAL</option><option>IMPORTANT</option><option>MANUAL_ONLY</option></select></label>
      <label>回复时段<select value={draft.reply_time_window_enabled} onChange={e=>setDraft({...draft,reply_time_window_enabled:e.target.value})}><option value="inherit">继承全局（当前 {detail.effective_policy.reply_time_window_enabled?'开启':'关闭'}）</option><option value="true">此联系人开启</option><option value="false">此联系人关闭</option></select></label>
      <label>开始时间（北京时间）<input type="time" value={draft.reply_auto_start} placeholder={detail.effective_policy.reply_auto_start} onChange={e=>setDraft({...draft,reply_auto_start:e.target.value})}/></label>
      <label>结束时间（北京时间）<input type="time" value={draft.reply_auto_end} placeholder={detail.effective_policy.reply_auto_end} onChange={e=>setDraft({...draft,reply_auto_end:e.target.value})}/></label>
      <label>媒体保存<select value={draft.media_storage_enabled} onChange={e=>setDraft({...draft,media_storage_enabled:e.target.value})}><option value="inherit">继承全局（当前 {detail.effective_policy.media_storage_enabled?'开启':'关闭'}）</option><option value="true">此联系人开启</option><option value="false">此联系人关闭</option></select></label>
      <label>媒体 AI 回复<select value={draft.media_ai_reply_enabled} onChange={e=>setDraft({...draft,media_ai_reply_enabled:e.target.value})}><option value="inherit">继承全局（当前 {detail.effective_policy.media_ai_reply_enabled?'开启':'关闭'}）</option><option value="true">此联系人开启</option><option value="false">此联系人关闭</option></select></label>
      <label>续火时间（北京时间）<input type="time" value={draft.keepalive_time} onChange={e=>setDraft({...draft,keepalive_time:e.target.value})}/></label>
      <label>生日（MM-DD）<input value={draft.birthday_mmdd} onChange={e=>setDraft({...draft,birthday_mmdd:e.target.value})} placeholder="例如 08-30" pattern="\d{2}-\d{2}"/></label>
      <label>长期未回复提醒（天）<input type="number" min={3} max={365} value={draft.dormant_reminder_days} onChange={e=>setDraft({...draft,dormant_reminder_days:Number(e.target.value)})}/></label>
      <label className="switch-field"><Switch checked={draft.relationship_reminders_enabled} label="关系提醒" onChange={value=>setDraft({...draft,relationship_reminders_enabled:value})}/><span>只在后台提醒管理员</span></label>
      <label className="wide-field">回复风格<textarea rows={3} value={draft.style_profile} onChange={e=>setDraft({...draft,style_profile:e.target.value})}/></label>
      <label className="wide-field">联系人独立提示词<textarea rows={3} value={draft.custom_prompt} onChange={e=>setDraft({...draft,custom_prompt:e.target.value})}/></label>
      <button className="primary-button compact" disabled={busy}>{busy?'保存中…':'保存详细策略'}</button>
    </form>
    <div className="contact-detail-columns"><div><h4>最近聊天</h4>{detail.recent_messages.length?detail.recent_messages.map(row=><article key={row.id}><span>{row.author} · {row.status}</span><p>{row.content}</p><small>{friendlyTime(row.created_at)}</small></article>):<p className="hint">暂无消息</p>}</div><div><h4>最近异常</h4>{detail.recent_anomalies.length?detail.recent_anomalies.map((row,index)=><article className="anomaly" key={`${row.event}-${index}`}><span>{row.level} · {row.event}</span><code>{JSON.stringify(row.detail)}</code><small>{friendlyTime(row.created_at)}</small></article>):<p className="hint">没有发现异常</p>}</div></div>
  </section>;
}

function GroupsPanel({groups,token,refresh,notify}:{groups:Group[];token:string;refresh:()=>Promise<void>;notify:(s:string)=>void}) {
  const [form,setForm]=useState({platform:'SIMULATOR',platform_group_id:'',display_name:'',allowed:false,ai_enabled:false});
  async function create(e:FormEvent){e.preventDefault();try{await api('/groups',token,{method:'POST',body:JSON.stringify(form)});setForm({...form,platform_group_id:'',display_name:''});await refresh();notify('群聊已创建，默认禁止自动回复')}catch(x){notify(x instanceof Error?x.message:'创建失败')}}
  async function patch(id:string,data:Record<string,boolean>){try{await api(`/groups/${id}`,token,{method:'PATCH',body:JSON.stringify(data)});await refresh();notify('群聊规则已更新')}catch(x){notify(x instanceof Error?x.message:'更新失败')}}
  return <div className="page-stack"><section className="callout"><strong>群聊双重许可</strong><p>只有“群聊位于允许列表”并且“当前消息明确 @你”时，才可能进入 AI 管线。缺少任一条件都只记录、不回复。</p></section><section className="panel"><div className="panel-heading"><h3>添加群聊</h3></div><form className="form-grid" onSubmit={create}><label>平台<select value={form.platform} onChange={e=>setForm({...form,platform:e.target.value})}><option>SIMULATOR</option><option value="QQ">QQ 官方</option><option value="QQ_NAPCAT">QQ · NapCat</option><option value="WECHAT_AUTOWX">微信 · AutoWx 草稿</option><option value="WECHAT">微信官方门禁</option></select></label><label>群聊 ID<input value={form.platform_group_id} onChange={e=>setForm({...form,platform_group_id:e.target.value})} required/></label><label>名称<input value={form.display_name} onChange={e=>setForm({...form,display_name:e.target.value})} required/></label><button className="primary-button align-end">添加群聊</button></form></section><section className="panel table-panel">{groups.length?<div className="data-table three"><div className="table-row table-head"><span>群聊</span><span>允许列表</span><span>AI 回复</span></div>{groups.map(item=><div className="table-row" key={item.id}><span><b>{item.display_name}</b><small>{item.platform} · {item.platform_group_id}</small></span><span><Switch checked={item.allowed} label="允许" onChange={v=>void patch(item.id,{allowed:v})}/></span><span><Switch checked={item.ai_enabled} label="AI" onChange={v=>void patch(item.id,{ai_enabled:v})}/></span></div>)}</div>:<EmptyState title="暂无群聊" detail="系统不会发现或加入任何群聊，只有你手动配置的群聊才会出现。"/>}</section></div>;
}

function ConversationsPanel({conversations,generating,token,refresh,notify}:{conversations:Conversation[];generating:Set<string>;token:string;refresh:()=>Promise<void>;notify:(s:string)=>void}) {
  const [selected,setSelected]=useState<string>(''); const [messages,setMessages]=useState<ChatMessage[]>([]); const [summary,setSummary]=useState<ConversationSummary|null>(null); const [diagnostic,setDiagnostic]=useState<Diagnostic|null>(null); const [reference,setReference]=useState<ReferencePreview|null>(null); const [hasOlder,setHasOlder]=useState(false); const [loadingMessages,setLoadingMessages]=useState(false); const [showLatest,setShowLatest]=useState(false);
  const [manualText,setManualText]=useState(''); const [manualSending,setManualSending]=useState(false);
  const streamRef=useRef<HTMLDivElement|null>(null); const loadingOlderRef=useRef(false); const requestRef=useRef(0); const selectedRef=useRef(''); const lastActivityRef=useRef(''); const stickToBottomRef=useRef(true); const scrollIntentRef=useRef<{kind:'bottom'}|{kind:'preserve';height:number;top:number}|null>(null);
  const current=conversations.find(item=>item.id===selected);
  const currentActivity=current?.last_active_at??'';
  const fetchPage=useCallback(async(id:string,before?:ChatMessage)=>{const params=new URLSearchParams({limit:String(CONVERSATION_PAGE_SIZE+1)});if(before){params.set('before_created_at',before.created_at);params.set('before_id',before.id)}const raw=await api<ChatMessage[]>(`/conversations/${id}/messages?${params.toString()}`,token);return {items:raw.slice(-CONVERSATION_PAGE_SIZE),hasMore:raw.length>CONVERSATION_PAGE_SIZE}},[token]);
  const loadInitial=useCallback(async(id:string)=>{const request=++requestRef.current;setLoadingMessages(true);try{const [page,nextSummary]=await Promise.all([fetchPage(id),api<ConversationSummary>(`/conversations/${id}/summary`,token)]);if(request!==requestRef.current)return;scrollIntentRef.current={kind:'bottom'};stickToBottomRef.current=true;setShowLatest(false);setMessages(page.items);setHasOlder(page.hasMore);setSummary(nextSummary)}catch(e){notify(e instanceof Error?e.message:'读取失败')}finally{if(request===requestRef.current)setLoadingMessages(false)}},[fetchPage,notify,token]);
  const refreshLatest=useCallback(async(id:string)=>{try{const page=await fetchPage(id);if(selectedRef.current!==id)return;const shouldFollow=stickToBottomRef.current;setMessages(existing=>{const merged=new Map(existing.map(item=>[item.id,item]));page.items.forEach(item=>merged.set(item.id,item));return [...merged.values()].sort((left,right)=>apiDate(left.created_at).getTime()-apiDate(right.created_at).getTime()||left.id.localeCompare(right.id))});if(shouldFollow)scrollIntentRef.current={kind:'bottom'};else setShowLatest(true);const nextSummary=await api<ConversationSummary>(`/conversations/${id}/summary`,token);if(selectedRef.current===id)setSummary(nextSummary)}catch(e){notify(e instanceof Error?e.message:'新消息读取失败')}},[fetchPage,notify,token]);
  function choose(id:string){requestRef.current+=1;selectedRef.current=id;setSelected(id);setMessages([]);setSummary(null);setDiagnostic(null);setReference(null);setHasOlder(false);setShowLatest(false);setManualText('');stickToBottomRef.current=true;lastActivityRef.current=conversations.find(item=>item.id===id)?.last_active_at??''}
  useEffect(()=>{const timer=window.setTimeout(()=>{if(conversations.length===0){selectedRef.current='';setSelected('');setMessages([]);return}if(!selected||!conversations.some(item=>item.id===selected)){const first=conversations[0];requestRef.current+=1;selectedRef.current=first.id;setSelected(first.id);setMessages([]);setSummary(null);setHasOlder(false);setShowLatest(false);stickToBottomRef.current=true;lastActivityRef.current=first.last_active_at}},0);return()=>window.clearTimeout(timer)},[conversations,selected]);
  useEffect(()=>{if(!selected)return;const timer=window.setTimeout(()=>void loadInitial(selected),0);return()=>window.clearTimeout(timer)},[loadInitial,selected]);
  useEffect(()=>{if(!selected||!currentActivity||currentActivity===lastActivityRef.current)return;lastActivityRef.current=currentActivity;void refreshLatest(selected)},[currentActivity,refreshLatest,selected]);
  useEffect(()=>{const node=streamRef.current;if(!node||typeof ResizeObserver==='undefined')return;const observer=new ResizeObserver(()=>{if(stickToBottomRef.current)node.scrollTop=node.scrollHeight});observer.observe(node);return()=>observer.disconnect()},[selected]);
  useLayoutEffect(()=>{const node=streamRef.current;const intent=scrollIntentRef.current;if(!node||!intent)return;scrollIntentRef.current=null;if(intent.kind==='bottom'){node.scrollTop=node.scrollHeight;return}node.scrollTop=intent.top+(node.scrollHeight-intent.height)},[messages]);
  async function loadOlder(){const first=messages[0],node=streamRef.current,id=selected;if(!id||!first||!node||!hasOlder||loadingOlderRef.current)return;loadingOlderRef.current=true;setLoadingMessages(true);stickToBottomRef.current=false;scrollIntentRef.current={kind:'preserve',height:node.scrollHeight,top:node.scrollTop};try{const page=await fetchPage(id,first);if(selectedRef.current!==id)return;setMessages(existing=>[...page.items.filter(item=>!existing.some(row=>row.id===item.id)),...existing]);setHasOlder(page.hasMore)}catch(e){scrollIntentRef.current=null;notify(e instanceof Error?e.message:'较早消息读取失败')}finally{loadingOlderRef.current=false;if(selectedRef.current===id)setLoadingMessages(false)}}
  function onMessageScroll(event:UIEvent<HTMLDivElement>){const node=event.currentTarget;const atBottom=node.scrollHeight-node.scrollTop-node.clientHeight<90;stickToBottomRef.current=atBottom;setShowLatest(!atBottom);if(node.scrollTop<100&&hasOlder&&!loadingOlderRef.current)void loadOlder()}
  function jumpLatest(){const node=streamRef.current;if(!node)return;stickToBottomRef.current=true;setShowLatest(false);node.scrollTo({top:node.scrollHeight,behavior:'smooth'})}
  async function diagnose(id:string){try{setDiagnostic(await api<Diagnostic>(`/messages/${id}/diagnostics`,token));setReference(null)}catch(e){notify(e instanceof Error?e.message:'诊断失败')}}
  async function previewReference(id:string){try{setReference(await api<ReferencePreview>(`/messages/${id}/reference-preview`,token));setDiagnostic(null)}catch(e){notify(e instanceof Error?e.message:'引用读取失败')}}
  async function action(kind:'takeover'|'resume'){if(!selected)return;try{await api(`/conversations/${selected}/${kind}`,token,{method:'POST'});await refresh();await loadInitial(selected);notify(kind==='takeover'?'已人工接管，队列消息已取消':'AI 已恢复待命，只会响应下一条新消息')}catch(e){notify(e instanceof Error?e.message:'操作失败')}}
  async function manualSend(event:FormEvent){event.preventDefault();const content=manualText.trim();if(!selected||!content||manualSending)return;setManualSending(true);try{await api(`/conversations/${selected}/manual-send`,token,{method:'POST',body:JSON.stringify({content})});setManualText('');stickToBottomRef.current=true;scrollIntentRef.current={kind:'bottom'};await Promise.all([refresh(),loadInitial(selected)]);notify('人工消息已发送；AI 托管状态未改变')}catch(e){notify(e instanceof Error?e.message:'人工发送失败')}finally{setManualSending(false)}}
  const manualSupported=current?.platform==='QQ_NAPCAT'&&current.whitelisted&&Boolean(current.account_id);
  return <div className="page-stack"><section className="conversation-console"><aside><div className="console-title"><p className="eyebrow">ACTIVE THREADS</p><h3>会话</h3></div>{conversations.length?conversations.map(item=><button key={item.id} className={selected===item.id?'selected':''} onClick={()=>choose(item.id)}><span className="contact-avatar">{item.display_name.slice(0,1)}</span><div><strong>{item.display_name}</strong><p>{item.last_message||'系统事件'}</p><small>{generating.has(item.id)?'正在生成回复…':`${item.mode} · ${friendlyTime(item.last_active_at)}`}</small></div></button>):<EmptyState title="暂无会话" detail="模拟消息或受支持通道的入站消息会出现在这里。"/>}</aside><article>{current?<><header><div><p className="eyebrow">{current.platform} CONVERSATION</p><h3>{current.display_name}</h3><span className="mode-line">{current.mode} · 连续托管 {current.managed_rounds} 轮</span>{generating.has(current.id)&&<span className="generating-chip">Generating… 正在生成</span>}</div><div><button className="secondary-button" onClick={()=>void action('takeover')}>立即接管</button><button className="primary-button compact" onClick={()=>void action('resume')}>恢复 AI</button></div></header><dl className="conversation-facts"><div><dt>AI</dt><dd>{current.ai_enabled?'已启用':'已关闭'}</dd></div><div><dt>白名单</dt><dd>{current.whitelisted?'允许':'禁止'}</dd></div><div><dt>重要度</dt><dd>{current.importance}</dd></div><div><dt>最近模型</dt><dd>{current.model}</dd></div><div><dt>记忆</dt><dd>{current.memory_enabled?'已启用':'已关闭'}</dd></div><div><dt>最后活跃</dt><dd>{friendlyTime(current.last_active_at)}</dd></div></dl>{summary?.content&&<details className="conversation-summary"><summary>查看本地会话摘要 <span>{summary.created_at?friendlyTime(summary.created_at):''}</span></summary><pre>{summary.content}</pre></details>}<div ref={streamRef} className="message-stream" onScroll={onMessageScroll}>{hasOlder&&<button className="message-history-control" disabled={loadingMessages} onClick={()=>void loadOlder()}>{loadingMessages?'正在读取较早消息…':'加载较早消息'}</button>}{messages.map(message=><div key={message.id} className={`message-bubble ${message.author.toLowerCase()}`}><span>{message.author} · {message.message_type}</span>{message.author==='AI'&&<div className="model-attribution"><span>本条实际回复模型</span><strong>{message.provider||'Provider 未记录'}</strong><code>{message.model||'历史记录未标注具体模型'}</code></div>}<p>{message.content}</p>{(message.attachments??[]).length>0&&<div className="message-media">{message.attachments.map(item=><MediaAttachmentCard key={item.id} item={item} token={token}/>)}</div>}<small>{message.status} · {friendlyTime(message.event_at||message.created_at)}</small><div className="message-tools"><button onClick={()=>void diagnose(message.id)}>为什么没回复</button>{message.author==='CONTACT'&&<button onClick={()=>void previewReference(message.id)}>引用内容</button>}</div></div>)}{loadingMessages&&!messages.length&&<span className="message-loading">正在读取最新消息…</span>}{showLatest&&<button className="jump-latest" onClick={jumpLatest}>回到最新消息 ↓</button>}</div><form className="manual-send-composer" onSubmit={manualSend}><div><label htmlFor="manual-send-content">人工直发</label><small>{manualSupported?'立即通过当前托管账号发送，并记为本人消息；不会触发或延长人工接管等待。':'仅支持已绑定当前账号的 NapCat 白名单私聊。'}</small></div><textarea id="manual-send-content" rows={2} maxLength={4000} value={manualText} onChange={event=>setManualText(event.target.value)} onKeyDown={event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();event.currentTarget.form?.requestSubmit()}}} placeholder={manualSupported?'输入消息；Enter 发送，Shift + Enter 换行':'当前会话不可人工直发'} disabled={!manualSupported||manualSending}/><button className="primary-button compact" type="submit" disabled={!manualSupported||!manualText.trim()||manualSending}>{manualSending?'发送中…':'人工直发'}</button></form></>:<EmptyState title="选择一个会话" detail="查看完整消息轨迹，并随时人工接管。"/>}</article></section>{diagnostic&&<DiagnosticPanel value={diagnostic} close={()=>setDiagnostic(null)}/>} {reference&&<ReferencePanel value={reference} token={token} close={()=>setReference(null)}/>}</div>;
}

function DiagnosticPanel({value,close}:{value:Diagnostic;close:()=>void}) {
  return <section className="panel diagnostic-panel"><div className="panel-heading"><div><p className="eyebrow">MESSAGE TRACE</p><h3>为什么没回复</h3></div><button className="ghost-button" onClick={close}>关闭</button></div><div className={`diagnostic-outcome ${value.outcome.replied?'pass':'blocked'}`}><strong>{value.outcome.code}</strong><p>{value.outcome.reason}</p></div><div className="diagnostic-steps">{value.steps.map((item,index)=><article key={item.key} className={item.status.toLowerCase()}><span>{index+1}</span><div><strong>{item.label}</strong><p>{item.detail}</p></div><b>{item.status}</b></article>)}</div><details className="diagnostic-timeline"><summary>查看完整审计时间线（{value.timeline.length} 条）</summary>{value.timeline.map((item,index)=><article key={`${item.event}-${index}`}><span>{friendlyTime(item.created_at)}</span><strong>{item.event}</strong><code>{JSON.stringify(item.detail)}</code></article>)}</details></section>;
}

function ReferencePanel({value,token,close}:{value:ReferencePreview;token:string;close:()=>void}) {
  return <section className="panel reference-panel"><div className="panel-heading"><div><p className="eyebrow">REFERENCE EVIDENCE</p><h3>引用内容预览</h3></div><button className="ghost-button" onClick={close}>关闭</button></div>{value.has_reference&&value.message?<><div className="reference-source"><span>{value.source} · {value.message.author} · {value.message.message_type}</span><p>{value.message.content}</p><small>{friendlyTime(value.message.created_at)} · {value.message.external_message_id}</small></div>{value.attachments.length>0&&<div className="reference-attachments">{value.attachments.map(item=><MediaAttachmentCard key={item.id} item={item} token={token}/>)}</div>}</>:<EmptyState title="没有找到可验证的引用内容" detail={`请求引用 ID：${value.requested_external_id??'未提供'}。系统不会凭空猜测引用。`}/>}</section>;
}

function readableBytes(value?:number){if(value===undefined||value===null)return '大小未知';if(value<1024)return `${value} B`;if(value<1024*1024)return `${(value/1024).toFixed(1)} KB`;return `${(value/1024/1024).toFixed(1)} MB`}

function MediaAttachmentCard({item,token}:{item:MediaAttachment;token:string}) {
  const [objectUrl,setObjectUrl]=useState('');
  const previewable=item.status==='SAVED'&&Boolean(item.download_url)&&['IMAGE','AUDIO','VIDEO'].includes(item.kind);
  useEffect(()=>{
    if(!previewable||!item.download_url)return;
    const controller=new AbortController(); let created='';
    void fetch(`${API_BASE}${item.download_url}`,{headers:{Authorization:`Bearer ${token}`},signal:controller.signal}).then(response=>{if(!response.ok)throw new Error('媒体读取失败');return response.blob()}).then(blob=>{created=URL.createObjectURL(blob);setObjectUrl(created)}).catch(()=>setObjectUrl(''));
    return()=>{controller.abort();if(created)URL.revokeObjectURL(created)};
  },[item.download_url,previewable,token]);
  async function download(){if(!item.download_url)return;const response=await fetch(`${API_BASE}${item.download_url}`,{headers:{Authorization:`Bearer ${token}`}});if(!response.ok)return;const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=item.file_name;link.click();URL.revokeObjectURL(url)}
  const statusLabel=item.status==='SAVED'?'已保存':item.status==='SKIPPED_DISABLED'?'保存已关闭':item.status==='SKIPPED_TOO_LARGE'?'超过大小限制':'仅保存元数据';
  const analysisLabel=item.analysis_status==='COMPLETED'?'识别完成':item.analysis_status==='FAILED'?'识别失败':item.analysis_status==='UNAVAILABLE'?'文件不可用':item.analysis_status==='PROCESSING'?'识别中':item.analysis_status==='SKIPPED_TYPE'?'未启用此类型':'未识别';
  return <article className={`media-attachment ${item.kind.toLowerCase()} ${item.status==='SAVED'?'saved':'metadata'}`}>
    {item.kind==='IMAGE'&&objectUrl&&<img src={objectUrl} alt={item.file_name}/>} 
    {item.kind==='AUDIO'&&objectUrl&&<audio controls preload="metadata" src={objectUrl}/>} 
    {item.kind==='VIDEO'&&objectUrl&&<video controls preload="metadata" src={objectUrl}/>} 
    <div><span>{item.kind}</span><strong>{item.file_name}</strong><small>{readableBytes(item.size_bytes)} · {statusLabel} · {analysisLabel}</small></div>
    {item.status==='SAVED'&&<button type="button" onClick={()=>void download()}>下载</button>}
    {item.status!=='SAVED'&&item.error_code&&<div className="media-analysis failed"><span>保存失败</span><p>{item.error_code}{item.storage_attempts?.length?` · ${item.storage_attempts.join(' → ')}`:''}</p></div>}
    {item.analysis_text&&<div className="media-analysis"><span>本机识别 · {item.analysis_provider} / {item.analysis_model}</span><p>{item.analysis_text}</p></div>}
    {['FAILED','UNAVAILABLE'].includes(item.analysis_status)&&<div className="media-analysis failed"><span>识别暂不可用</span><p>{item.analysis_error_code}</p></div>}
  </article>;
}

function CacheStickerThumbnail({item,token,selected,toggle}:{item:StickerCacheCandidate;token:string;selected:boolean;toggle:()=>void}) {
  const [objectUrl,setObjectUrl]=useState('');
  useEffect(()=>{const controller=new AbortController();let created='';void fetch(`${API_BASE}/stickers/cache/${item.id}`,{headers:{Authorization:`Bearer ${token}`},signal:controller.signal}).then(response=>{if(!response.ok)throw new Error('缓存预览失败');return response.blob()}).then(blob=>{created=URL.createObjectURL(blob);setObjectUrl(created)}).catch(()=>setObjectUrl(''));return()=>{controller.abort();if(created)URL.revokeObjectURL(created)}},[item.id,token]);
  return <button type="button" className={`cache-sticker ${selected?'selected':''}`} onClick={toggle}>{objectUrl?<img src={objectUrl} alt="QQ 缓存表情预览"/>:<span>预览加载中</span>}<small>{item.source_category} · {readableBytes(item.size_bytes)}</small><b>{selected?'已选择':'选择导入'}</b></button>;
}

function StickerThumbnail({item,token}:{item:StickerAsset;token:string}) {
  const [objectUrl,setObjectUrl]=useState(''); const [expanded,setExpanded]=useState(false);
  useEffect(()=>{const controller=new AbortController();let created='';void fetch(`${API_BASE}/stickers/${item.id}/content`,{headers:{Authorization:`Bearer ${token}`},signal:controller.signal}).then(response=>{if(!response.ok)throw new Error('贴图预览失败');return response.blob()}).then(blob=>{created=URL.createObjectURL(blob);setObjectUrl(created)}).catch(()=>setObjectUrl(''));return()=>{controller.abort();if(created)URL.revokeObjectURL(created)}},[item.id,token]);
  return <button type="button" className={`sticker-preview ${expanded?'expanded':''}`} onClick={()=>setExpanded(value=>!value)} aria-label={`${item.label}，点击${expanded?'收起':'放大'}预览`}>{objectUrl?<img src={objectUrl} alt={`${item.label} 表情预览`}/>:<span>预览加载中</span>}<small>{expanded?'点击收起':'点击放大'}</small></button>;
}

function SimulatorPanel({contacts,token,refresh,notify}:{contacts:Contact[];token:string;refresh:()=>Promise<void>;notify:(s:string)=>void}) {
  const options=contacts.filter(item=>item.platform==='SIMULATOR'); const [contactId,setContactId]=useState(''); const [content,setContent]=useState('今天晚上吃什么？'); const [result,setResult]=useState<Record<string,unknown>|null>(null); const [busy,setBusy]=useState(false);
  const effectiveContactId=contactId||options[0]?.id||'';
  async function send(e:FormEvent){e.preventDefault();setBusy(true);try{const value=await api<Record<string,unknown>>('/simulator/events',token,{method:'POST',body:JSON.stringify({contact_id:effectiveContactId,content})});setResult(value);await refresh();notify(value.sent?'模拟回复已完成':'策略已正确阻止发送')}catch(x){notify(x instanceof Error?x.message:'演练失败')}finally{setBusy(false)}}
  return <div className="simulator-layout"><section className="panel"><div className="panel-heading"><div><p className="eyebrow">FAKE CHAT PLATFORM</p><h3>输入一条模拟消息</h3></div><span className="safe-badge">不会真实发送</span></div>{options.length?<form className="sim-form" onSubmit={send}><label>模拟联系人<select value={effectiveContactId} onChange={e=>setContactId(e.target.value)}>{options.map(item=><option key={item.id} value={item.id}>{item.display_name} · {item.whitelisted?'白名单':'非白名单'}</option>)}</select></label><label>对方发来<textarea rows={6} value={content} onChange={e=>setContent(e.target.value)} required/></label><button className="primary-button" disabled={busy}>{busy?'正在经过安全管线…':'运行完整安全演练'}</button></form>:<EmptyState title="先创建模拟联系人" detail="前往“联系人”创建 SIMULATOR 联系人，并按需加入白名单。"/>}</section><section className="panel pipeline-panel"><div className="panel-heading"><div><p className="eyebrow">DETERMINISTIC PIPELINE</p><h3>决策结果</h3></div></div>{result?<><div className={`result-banner ${result.sent?'pass':'deny'}`}><span>{result.sent?'PASS':'DENY'}</span><strong>{String(result.code)}</strong><p>{String(result.reason)}</p></div>{result.reply&&<div className="reply-preview"><div className="model-attribution light"><span>本条实际回复模型</span><strong>{String(result.provider||'Provider 未记录')}</strong><code>{String(result.model||'具体模型未记录')}</code></div><p>{String(result.reply)}</p></div>}</>:<div className="pipeline-steps">{['接收与去重','白名单 / 群聊 / 时段','联系人记忆隔离','LLM 生成','内容安全检查','限频与发送前复检'].map((item,index)=><div key={item}><span>{index+1}</span><p>{item}</p><b>等待</b></div>)}</div>}</section></div>;
}

function AccountsPanel({accounts,token,refresh,notify}:{accounts:Account[];token:string;refresh:()=>Promise<void>;notify:(s:string)=>void}) {
  const qq=accounts.find(item=>item.platform==='QQ');
  const napcats=accounts.filter(item=>item.platform==='QQ_NAPCAT');
  const autowx=accounts.find(item=>item.platform==='WECHAT_AUTOWX');
  const [appId,setAppId]=useState(qq?.app_id??'');
  const [secret,setSecret]=useState('');
  const [qqEnabled,setQqEnabled]=useState(qq?.enabled??false);
  const [newNapcat,setNewNapcat]=useState({display_name:'我的 NapCat 账号',managed_qq_id:'',api_base:'http://127.0.0.1:3001',access_token:'',risk_acknowledged:false});
  const [autowxToken,setAutowxToken]=useState('');
  const [autowxEnabled,setAutowxEnabled]=useState(autowx?.enabled??false);
  const [autowxRisk,setAutowxRisk]=useState(autowx?.risk_acknowledged??false);

  async function saveQQ(e:FormEvent){e.preventDefault();try{await api('/accounts/QQ',token,{method:'PUT',body:JSON.stringify({enabled:qqEnabled,app_id:appId,app_secret:secret||null})});setSecret('');await refresh();notify('QQ 官方 Bot 配置已保留，可稍后继续处理回调')}catch(x){notify(x instanceof Error?x.message:'保存失败')}}
  async function createNapcat(e:FormEvent){e.preventDefault();try{await api('/napcat/profiles',token,{method:'POST',body:JSON.stringify(newNapcat)});setNewNapcat({...newNapcat,display_name:'我的 NapCat 账号',managed_qq_id:'',access_token:'',risk_acknowledged:false});await refresh();notify('NapCat 账号配置已加密保存；需要时再激活')}catch(x){notify(x instanceof Error?x.message:'NapCat 账号创建失败')}}
  async function saveAutowx(e:FormEvent){e.preventDefault();try{await api('/accounts/WECHAT_AUTOWX',token,{method:'PUT',body:JSON.stringify({enabled:autowxEnabled,access_token:autowxToken||null,risk_acknowledged:autowxRisk})});setAutowxToken('');await refresh();notify(autowxEnabled?'AutoWx 接收桥已启用；自动发送仍被硬性禁止':'AutoWx 配置已保存并保持关闭')}catch(x){notify(x instanceof Error?x.message:'AutoWx 保存失败')}}

  return <div className="page-stack">
    <section className="callout warning"><strong>实验通道边界</strong><p>NapCat 与 AutoWx 都不是平台官方个人账号自动化方案，存在账号处罚、客户端升级失效和隐私风险。系统不会提供反检测；默认关闭，必须在本机、SHADOW 和单个测试联系人范围内逐级验证。</p></section>
    <section className="connector-card supported"><div className="connector-heading"><div className="big-platform">QQ</div><div><span className="safe-badge">OFFICIAL · 保留</span><h2>QQ 官方 Bot</h2><p>官方凭据与回调配置原样保留。当前可以先关闭，不再阻塞你试用 NapCat；V1 的“QQ 官方 200 条验收”仍只认这个通道。</p></div><b>{qq?.status??'DISCONNECTED'}</b></div><form className="form-grid" onSubmit={saveQQ}><label>AppID<input value={appId} onChange={e=>setAppId(e.target.value)} placeholder="QQ 开放平台 AppID"/></label><label>AppSecret<input type="password" value={secret} onChange={e=>setSecret(e.target.value)} placeholder={qq?.credential_present?'已加密保存；留空不修改':'输入 AppSecret'}/></label><label className="switch-field"><Switch checked={qqEnabled} label="启用官方连接器" onChange={setQqEnabled}/><span>暂不使用时可关闭，凭据不会删除。</span></label><button className="primary-button align-end">保存官方配置</button></form><div className="connector-guide"><strong>官方事件回调（稍后再处理）</strong><code>/api/v1/connectors/qq/webhook</code><small>这条路线没有被删除，只是暂时不再作为当前推进阻塞项。</small></div></section>

    <section className="connector-card experimental"><div className="connector-heading"><div className="big-platform">NC</div><div><span className="warning-badge">EXPERIMENTAL · ACCOUNT PROFILES</span><h2>NapCat 多账号切换</h2><p>后台可以保存多个 QQ 的独立地址和加密 Token。同一时刻只激活一个账号；切换会先停用旧账号并取消尚未发送的队列，避免错账号发送。</p></div><b>{napcats.find(item=>item.enabled)?.status??'NO ACTIVE'}</b></div><div className="napcat-profile-list">{napcats.map(item=><NapCatProfileCard key={item.id} item={item} token={token} refresh={refresh} notify={notify}/>)}</div><form className="napcat-profile-form" onSubmit={createNapcat}><div className="panel-heading compact-heading"><div><p className="eyebrow">NEW PROFILE</p><h3>新增 NapCat 账号</h3></div></div><label>配置名称<input value={newNapcat.display_name} onChange={e=>setNewNapcat({...newNapcat,display_name:e.target.value})} required/></label><label>托管 QQ 号<input value={newNapcat.managed_qq_id} onChange={e=>setNewNapcat({...newNapcat,managed_qq_id:e.target.value.replace(/\D/g,'')})} inputMode="numeric" required/></label><label>本机 OneBot 地址<input value={newNapcat.api_base} onChange={e=>setNewNapcat({...newNapcat,api_base:e.target.value})} required/></label><label>Access Token<input type="password" value={newNapcat.access_token} onChange={e=>setNewNapcat({...newNapcat,access_token:e.target.value})} minLength={12} required/></label><label className="switch-field"><Switch checked={newNapcat.risk_acknowledged} label="确认 NapCat 风险" onChange={value=>setNewNapcat({...newNapcat,risk_acknowledged:value})}/><span>确认非官方个人 QQ 自动化风险。</span></label><button className="primary-button">加密保存账号</button></form><div className="connector-guide"><strong>切换边界</strong><p>切换面板不会替你登录 QQ。请先在 NapCat 登录目标 QQ，并确认该账号自己的 HTTP Server / Client 配置已恢复，再点击对应配置的“激活”。</p><code>http://127.0.0.1:8000/api/v1/connectors/napcat/events</code><small>所有配置仍只允许回环地址。若未来真双开 NapCat，需要为每个实例分配不同端口并进一步隔离入站账号身份；当前版本选择更稳妥的单活切换。</small></div></section>

    <section className="connector-card experimental draft-only"><div className="connector-heading"><div className="big-platform">微</div><div><span className="warning-badge">EXPERIMENTAL · DRAFT ONLY</span><h2>AutoWx 托管策略</h2><p>只允许本机桥接程序上报已接收消息，由 Neko 生成影子草稿。后端没有 AutoWx 发送能力，即使误切到 LIVE 也会被策略硬性阻止。</p></div><b>{autowx?.status??'DISCONNECTED'}</b></div><form className="form-grid experimental-form" onSubmit={saveAutowx}><label>本地 Bridge Token<input type="password" value={autowxToken} onChange={e=>setAutowxToken(e.target.value)} minLength={12} placeholder={autowx?.credential_present?'已加密保存；留空不修改':'至少 12 位随机 Token'}/></label><label className="switch-field"><Switch checked={autowxRisk} label="确认 AutoWx 风险" onChange={setAutowxRisk}/><span>我理解 UI 自动化可能触发风控或失效。</span></label><label className="switch-field"><Switch checked={autowxEnabled} label="启用 AutoWx 接收" onChange={setAutowxEnabled}/><span>仅接收与生成草稿，禁止自动发送。</span></label><button className="primary-button align-end">加密保存 AutoWx</button></form><div className="connector-guide"><strong>本地桥接入口</strong><p>AutoWx 适配器需在本机把文本消息 POST 到下列地址，并携带 Bearer Token。接入契约稍后可按你实际安装的 AutoWx 版本制作。</p><code>http://127.0.0.1:8000/api/v1/connectors/autowx/events</code><small>当前策略不调用点击“发送”、不提供反检测，也不计入 V1 微信正式接入验收。</small></div></section>
  </div>;
}

function NapCatProfileCard({item,token,refresh,notify}:{item:Account;token:string;refresh:()=>Promise<void>;notify:(s:string)=>void}) {
  const [draft,setDraft]=useState({display_name:item.display_name,managed_qq_id:item.managed_qq_id??'',api_base:item.api_base??'http://127.0.0.1:3001',access_token:'',risk_acknowledged:item.risk_acknowledged});
  async function save(e:FormEvent){e.preventDefault();try{await api(`/napcat/profiles/${item.id}`,token,{method:'PUT',body:JSON.stringify({...draft,...(!draft.access_token?{access_token:null}:{})})});setDraft({...draft,access_token:''});await refresh();notify('NapCat 账号配置已更新')}catch(x){notify(x instanceof Error?x.message:'保存失败')}}
  async function activate(){if(!window.confirm(`确认切换到 ${draft.display_name}？旧账号会立即停用，待发队列会被取消。`))return;try{const result=await api<{activation_diagnostics?:{status:string;detail:string;suggestion:string}}>(`/napcat/profiles/${item.id}/activate`,token,{method:'POST'});await refresh();const check=result.activation_diagnostics;notify(check?.status==='OK'?'NapCat 活动账号已切换且 OneBot 检查通过':`账号已切换，但仍需处理：${check?.detail??'请前往可靠性中心查看缺失项'}`)}catch(x){notify(x instanceof Error?x.message:'切换失败')}}
  async function deactivate(){try{await api(`/napcat/profiles/${item.id}/deactivate`,token,{method:'POST'});await refresh();notify('NapCat 账号已停用')}catch(x){notify(x instanceof Error?x.message:'停用失败')}}
  async function remove(){if(!window.confirm(`删除 ${item.display_name} 的本机配置？已加密 Token 也会删除。`))return;try{await api(`/napcat/profiles/${item.id}`,token,{method:'DELETE'});await refresh();notify('NapCat 账号配置已删除')}catch(x){notify(x instanceof Error?x.message:'删除失败')}}
  return <form className={`napcat-profile ${item.enabled?'active':''}`} onSubmit={save}><header><div><span className={item.enabled?'safe-badge':'warning-badge'}>{item.enabled?'当前激活':'已保存'}</span><strong>{item.display_name}</strong><small>{item.status}</small></div><div className="record-actions">{item.enabled?<button type="button" className="secondary-button small" onClick={()=>void deactivate()}>停用</button>:<button type="button" className="primary-button compact" onClick={()=>void activate()}>激活此账号</button>}<button type="button" className="ghost-button danger" onClick={()=>void remove()}>删除</button></div></header><div className="napcat-profile-fields"><label>配置名称<input value={draft.display_name} onChange={e=>setDraft({...draft,display_name:e.target.value})} required/></label><label>托管 QQ 号<input value={draft.managed_qq_id} onChange={e=>setDraft({...draft,managed_qq_id:e.target.value.replace(/\D/g,'')})} inputMode="numeric" required/></label><label>本机 OneBot 地址<input value={draft.api_base} onChange={e=>setDraft({...draft,api_base:e.target.value})} required/></label><label>更新 Token<input type="password" value={draft.access_token} onChange={e=>setDraft({...draft,access_token:e.target.value})} minLength={12} placeholder={item.credential_present?'已加密；留空不修改':'至少 12 位'}/></label><label className="switch-field"><Switch checked={draft.risk_acknowledged} label="风险确认" onChange={value=>setDraft({...draft,risk_acknowledged:value})}/><span>确认风险</span></label><button className="secondary-button">保存修改</button></div></form>;
}

function providerDefaults(providerType:string) {
  if(providerType==='KIMI')return {name:'Kimi Cloud',base_url:'https://api.moonshot.cn/v1',model:'kimi-k3',priority:10,timeout_seconds:120};
  if(providerType==='QWEN')return {name:'Qwen Cloud',base_url:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:'qwen-plus',priority:30,timeout_seconds:30};
  if(providerType==='OLLAMA')return {name:'Ollama Local',base_url:'http://127.0.0.1:11434',model:'llama3.1:8b',priority:100,timeout_seconds:120};
  if(providerType==='OPENAI')return {name:'OpenAI',base_url:'https://api.openai.com/v1',model:'gpt-4.1-mini',priority:40,timeout_seconds:30};
  if(providerType==='OPENAI_COMPATIBLE')return {name:'OpenAI Compatible',base_url:'https://example.com/v1',model:'model-name',priority:60,timeout_seconds:30};
  return {name:'DeepSeek',base_url:'https://api.deepseek.com',model:'deepseek-v4-flash',priority:20,timeout_seconds:25};
}

function credentialsForProvider(credentials:Credential[],providerType:string) {
  if(providerType==='OLLAMA')return [];
  if(providerType==='OPENAI_COMPATIBLE')return credentials.filter(item=>!['QQ','QQ_NAPCAT','WECHAT_AUTOWX'].includes(item.provider));
  return credentials.filter(item=>item.provider===providerType);
}

function AiPanel({providers,credentials,persona,token,refresh,notify}:{providers:Provider[];credentials:Credential[];persona:Persona|null;token:string;refresh:()=>Promise<void>;notify:(s:string)=>void}) {
  const [keyForm,setKeyForm]=useState({label:'DeepSeek Key',provider:'DEEPSEEK',secret:''});
  const [modelForm,setModelForm]=useState({name:'DeepSeek',provider_type:'DEEPSEEK',base_url:'https://api.deepseek.com',model:'deepseek-v4-flash',credential_id:'',priority:20,timeout_seconds:25,enabled:true});
  const [profile,setProfile]=useState<Persona|null>(persona);
  const compatibleCredentials=credentialsForProvider(credentials,modelForm.provider_type);
  const selectedCredentialId=modelForm.credential_id||compatibleCredentials[0]?.id||'';
  async function addKey(e:FormEvent){e.preventDefault();try{await api('/credentials',token,{method:'POST',body:JSON.stringify(keyForm)});setKeyForm({...keyForm,secret:''});await refresh();notify('API Key 已使用项目本机密钥加密保存')}catch(x){notify(x instanceof Error?x.message:'保存失败')}}
  async function addProvider(e:FormEvent){e.preventDefault();try{const payload={...modelForm,credential_id:modelForm.provider_type==='OLLAMA'?null:selectedCredentialId};await api('/providers',token,{method:'POST',body:JSON.stringify(payload)});await refresh();notify('模型配置已添加')}catch(x){notify(x instanceof Error?x.message:'保存失败')}}
  async function test(id:string){try{const result=await api<{ok:boolean;detail:string}>(`/providers/${id}/test`,token,{method:'POST'});notify(result.detail)}catch(x){notify(x instanceof Error?x.message:'测试失败')}}
  async function reorder(orderedIds:string[],strategy:'MANUAL'|'CAPABILITY'='MANUAL'){try{await api('/providers/reorder',token,{method:'POST',body:JSON.stringify({strategy,ordered_ids:orderedIds})});await refresh();notify(strategy==='CAPABILITY'?'已按模型能力重排；下一条新消息立即使用新顺序':'模型顺位已更新；下一条新消息立即使用新顺序')}catch(x){notify(x instanceof Error?x.message:'排序失败')}}
  async function moveProvider(id:string,direction:-1|1){const ordered=[...providers].sort((a,b)=>a.priority-b.priority);const index=ordered.findIndex(item=>item.id===id);const target=index+direction;if(index<0||target<0||target>=ordered.length)return;[ordered[index],ordered[target]]=[ordered[target],ordered[index]];await reorder(ordered.map(item=>item.id))}
  async function recommendOrder(){if(!window.confirm('按能力推荐顺序重排全部模型？启用项会依次尝试，失败后才进入下一项。'))return;await reorder([],'CAPABILITY')}
  async function savePersona(e:FormEvent){e.preventDefault();if(!profile)return;try{await api('/persona',token,{method:'PUT',body:JSON.stringify(profile)});await refresh();notify('人格配置已保存；安全规则仍具有更高优先级')}catch(x){notify(x instanceof Error?x.message:'保存失败')}}
  return <div className="page-stack">
    <section className="two-column">
      <div className="panel">
        <div className="panel-heading"><div><p className="eyebrow">ENCRYPTED CREDENTIALS</p><h3>API Key</h3></div><span className="safe-badge">本机密钥 · 不绑定 Windows 用户</span></div>
        <form className="stack-form" onSubmit={addKey}><label>标签<input value={keyForm.label} onChange={e=>setKeyForm({...keyForm,label:e.target.value})}/></label><label>提供商<select value={keyForm.provider} onChange={e=>setKeyForm({...keyForm,provider:e.target.value})}><option>DEEPSEEK</option><option>KIMI</option><option>QWEN</option><option>OPENAI</option><option>OTHER</option></select></label><label>API Key<input type="password" value={keyForm.secret} onChange={e=>setKeyForm({...keyForm,secret:e.target.value})} required minLength={6} placeholder="只在提交时进入本地后端"/></label><button className="primary-button">加密保存</button></form>
        <p className="window-safety-note">凭据随本项目数据目录保存，可从管理员或普通终端稳定重启；无需因网络连接失败重复填写相同 Token。</p>
        <div className="credential-list">{credentials.map(item=><CredentialRow key={item.id} item={item} token={token} refresh={refresh} notify={notify}/>)}</div>
      </div>
      <div className="panel">
        <div className="panel-heading provider-queue-heading"><div><p className="eyebrow">PROVIDER FALLBACK</p><h3>模型优先队列</h3><span className="hint">从第 1 顺位开始；失败后才尝试下一项，禁用项自动跳过</span></div><button type="button" className="secondary-button" onClick={()=>void recommendOrder()}>按能力推荐排序</button></div>
        <form className="stack-form" onSubmit={addProvider}><div className="form-grid"><label>名称<input value={modelForm.name} onChange={e=>setModelForm({...modelForm,name:e.target.value})}/></label><label>类型<select value={modelForm.provider_type} onChange={e=>{const provider_type=e.target.value;setModelForm({...modelForm,...providerDefaults(provider_type),provider_type,credential_id:''})}}><option>DEEPSEEK</option><option>KIMI</option><option>QWEN</option><option>OPENAI</option><option>OLLAMA</option><option>OPENAI_COMPATIBLE</option></select></label><label>Base URL<input value={modelForm.base_url} onChange={e=>setModelForm({...modelForm,base_url:e.target.value})}/></label><label>模型<input value={modelForm.model} onChange={e=>setModelForm({...modelForm,model:e.target.value})}/></label>{modelForm.provider_type!=='OLLAMA'&&<label>加密凭据<select value={selectedCredentialId} onChange={e=>setModelForm({...modelForm,credential_id:e.target.value})}><option value="">请选择</option>{compatibleCredentials.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}</select></label>}<button className="primary-button align-end">添加模型</button></div></form>
        <div className="provider-list">{providers.map((item,index)=><ProviderRow key={item.id} item={item} rank={index+1} total={providers.length} credentials={credentials} token={token} refresh={refresh} notify={notify} testProvider={()=>test(item.id)} move={direction=>moveProvider(item.id,direction)}/>)}</div>
      </div>
    </section>
    <section className="panel"><div className="panel-heading"><div><p className="eyebrow">PERSONA LAYERS</p><h3>用户风格 + 轻猫咪人格</h3></div><span className="safe-badge">Safety 永远优先</span></div>{profile&&<form className="persona-grid" onSubmit={savePersona}><label>用户聊天风格<textarea rows={5} value={profile.user_style} onChange={e=>setProfile({...profile,user_style:e.target.value})}/></label><label>全局猫咪人格<textarea rows={5} value={profile.global_persona} onChange={e=>setProfile({...profile,global_persona:e.target.value})}/></label><label>补充安全政策（只能加强硬规则）<textarea rows={5} value={profile.safety_policy} onChange={e=>setProfile({...profile,safety_policy:e.target.value})}/></label><button className="primary-button align-end">保存人格</button></form>}</section>
  </div>;
}

function CredentialRow({item,token,refresh,notify}:{item:Credential;token:string;refresh:()=>Promise<void>;notify:(s:string)=>void}) {
  const [editing,setEditing]=useState(false); const [label,setLabel]=useState(item.label); const [secret,setSecret]=useState('');
  function begin(){setLabel(item.label);setSecret('');setEditing(true)}
  async function save(e:FormEvent){e.preventDefault();const body:Record<string,string>={label};if(secret)body.secret=secret;try{await api(`/credentials/${item.id}`,token,{method:'PUT',body:JSON.stringify(body)});setEditing(false);setSecret('');await refresh();notify(secret?'API Key 已使用本机密钥轮换':'凭据标签已更新')}catch(x){notify(x instanceof Error?x.message:'更新失败')}}
  async function remove(){if(!window.confirm(`删除凭据“${item.label}”？仍被模型或账号引用时系统会拒绝删除。`))return;try{await api(`/credentials/${item.id}`,token,{method:'DELETE'});await refresh();notify('凭据已删除')}catch(x){notify(x instanceof Error?x.message:'删除失败')}}
  return <div className="admin-record"><div className="record-summary"><span>{item.provider}</span><div><strong>{item.label}</strong><code>{item.masked_hint}</code></div><div className="record-actions"><button type="button" className="secondary-button small" onClick={begin}>修改</button><button type="button" className="ghost-button danger" onClick={()=>void remove()}>删除</button></div></div>{editing&&<form className="record-editor" onSubmit={save}><label>标签<input value={label} onChange={e=>setLabel(e.target.value)} required/></label><label>新 API Key（留空不轮换）<input type="password" value={secret} onChange={e=>setSecret(e.target.value)} minLength={6}/></label><div className="record-actions"><button className="primary-button compact">保存</button><button type="button" className="ghost-button" onClick={()=>setEditing(false)}>取消</button></div></form>}</div>;
}

function ProviderRow({item,rank,total,credentials,token,refresh,notify,testProvider,move}:{item:Provider;rank:number;total:number;credentials:Credential[];token:string;refresh:()=>Promise<void>;notify:(s:string)=>void;testProvider:()=>Promise<void>;move:(direction:-1|1)=>Promise<void>}) {
  const [editing,setEditing]=useState(false);
  const [form,setForm]=useState({...item,credential_id:item.credential_id??''});
  const compatibleCredentials=credentialsForProvider(credentials,form.provider_type);
  function begin(){setForm({...item,credential_id:item.credential_id??''});setEditing(true)}
  async function save(e:FormEvent){e.preventDefault();const body={...form,credential_id:form.provider_type==='OLLAMA'?null:form.credential_id,priority:Number(form.priority),timeout_seconds:Number(form.timeout_seconds)};try{await api(`/providers/${item.id}`,token,{method:'PUT',body:JSON.stringify(body)});setEditing(false);await refresh();notify('模型配置已更新，下一条新消息按新顺序使用')}catch(x){notify(x instanceof Error?x.message:'更新失败')}}
  async function toggle(){try{await api(`/providers/${item.id}`,token,{method:'PUT',body:JSON.stringify({enabled:!item.enabled})});await refresh();notify(item.enabled?'模型已停用':'模型已启用')}catch(x){notify(x instanceof Error?x.message:'切换失败')}}
  async function remove(){if(!window.confirm(`删除模型配置“${item.name}”？`))return;try{await api(`/providers/${item.id}`,token,{method:'DELETE'});await refresh();notify('模型配置已删除')}catch(x){notify(x instanceof Error?x.message:'删除失败')}}
  return <div className={`admin-record provider-queue-item ${item.enabled?'enabled':'disabled'}`}><div className="record-summary provider-summary"><span className="provider-rank">#{rank}</span><span className={`provider-state ${item.enabled?'on':''}`}/><div><strong>{item.name}</strong><p>{item.provider_type} · <b>{item.model}</b></p></div><code>顺位 {rank} · P{item.priority}</code><div className="record-actions"><button type="button" className="queue-button" aria-label={`${item.name} 上移`} disabled={rank===1} onClick={()=>void move(-1)}>↑</button><button type="button" className="queue-button" aria-label={`${item.name} 下移`} disabled={rank===total} onClick={()=>void move(1)}>↓</button><button type="button" className="secondary-button small" onClick={()=>void testProvider()}>测试</button><button type="button" className="secondary-button small" onClick={begin}>修改</button><button type="button" className="ghost-button" onClick={()=>void toggle()}>{item.enabled?'停用':'启用'}</button><button type="button" className="ghost-button danger" onClick={()=>void remove()}>删除</button></div></div>{editing&&<form className="record-editor provider-editor" onSubmit={save}><label>名称<input value={form.name} onChange={e=>setForm({...form,name:e.target.value})} required/></label><label>类型<select value={form.provider_type} onChange={e=>{const provider_type=e.target.value;const defaults=providerDefaults(provider_type);setForm({...form,provider_type,base_url:defaults.base_url,model:defaults.model,credential_id:provider_type==='OLLAMA'?'':form.credential_id})}}><option>DEEPSEEK</option><option>KIMI</option><option>QWEN</option><option>OPENAI</option><option>OLLAMA</option><option>OPENAI_COMPATIBLE</option></select></label><label>Base URL<input value={form.base_url} onChange={e=>setForm({...form,base_url:e.target.value})} required/></label><label>模型<input value={form.model} onChange={e=>setForm({...form,model:e.target.value})} required/></label>{form.provider_type!=='OLLAMA'&&<label>加密凭据<select value={form.credential_id} onChange={e=>setForm({...form,credential_id:e.target.value})} required><option value="">请选择</option>{compatibleCredentials.map(value=><option key={value.id} value={value.id}>{value.label}</option>)}</select></label>}<label>优先级<input type="number" min={1} max={1000} value={form.priority} onChange={e=>setForm({...form,priority:Number(e.target.value)})}/></label><label>超时（秒）<input type="number" min={3} max={120} value={form.timeout_seconds} onChange={e=>setForm({...form,timeout_seconds:Number(e.target.value)})}/></label><div className="record-actions"><button className="primary-button compact">保存</button><button type="button" className="ghost-button" onClick={()=>setEditing(false)}>取消</button></div></form>}</div>;
}

function WorkspacePanel({contacts,token,notify}:{contacts:Contact[];token:string;notify:(s:string)=>void}) {
  const [tasks,setTasks]=useState<TaskItem[]>([]); const [recovery,setRecovery]=useState<RecoveryItem[]>([]); const [knowledge,setKnowledge]=useState<KnowledgeDoc[]>([]); const [todos,setTodos]=useState<TodoItem[]>([]); const [digests,setDigests]=useState<DailyDigest[]>([]); const [stickers,setStickers]=useState<StickerAsset[]>([]); const [routing,setRouting]=useState<RoutingInfo|null>(null); const [busy,setBusy]=useState(false);
  const [calendar,setCalendar]=useState<CalendarTool|null>(null); const [weather,setWeather]=useState<WeatherTool|null>(null); const [searchResult,setSearchResult]=useState<SearchTool|null>(null);
  const [weatherLocation,setWeatherLocation]=useState(''); const [searchQuery,setSearchQuery]=useState('');
  const [knowledgeTitle,setKnowledgeTitle]=useState(''); const [knowledgeContent,setKnowledgeContent]=useState('');
  const [todoTitle,setTodoTitle]=useState(''); const [todoDue,setTodoDue]=useState(''); const [todoContact,setTodoContact]=useState('');
  const [stickerLabel,setStickerLabel]=useState(''); const [stickerTags,setStickerTags]=useState(''); const [stickerFile,setStickerFile]=useState<File|null>(null);
  const [cacheCandidates,setCacheCandidates]=useState<StickerCacheCandidate[]>([]); const [cacheRoots,setCacheRoots]=useState<string[]>([]); const [selectedCache,setSelectedCache]=useState<string[]>([]);
  const load=useCallback(async()=>{try{const [taskData,recoveryData,knowledgeData,todoData,digestData,stickerData,routingData,calendarData]=await Promise.all([api<TaskItem[]>('/tasks',token),api<RecoveryItem[]>('/recovery?status=ALL',token),api<KnowledgeDoc[]>('/knowledge',token),api<TodoItem[]>('/todos',token),api<DailyDigest[]>('/digests',token),api<StickerAsset[]>('/stickers',token),api<RoutingInfo>('/model-routing',token),api<CalendarTool>('/tools/calendar',token)]);setTasks(taskData);setRecovery(recoveryData);setKnowledge(knowledgeData);setTodos(todoData);setDigests(digestData);setStickers(stickerData);setRouting(routingData);setCalendar(calendarData)}catch(x){notify(x instanceof Error?x.message:'工作台读取失败')}},[notify,token]);
  useEffect(()=>{const timer=window.setTimeout(()=>void load(),0);return()=>window.clearTimeout(timer)},[load]);
  async function act(work:()=>Promise<unknown>,message:string){setBusy(true);try{await work();await load();notify(message)}catch(x){notify(x instanceof Error?x.message:'操作失败')}finally{setBusy(false)}}
  async function queue(kind:'DAILY_DIGEST'|'KNOWLEDGE_REFRESH'|'RECOVERY_SYNC',title:string){await act(()=>api('/tasks',token,{method:'POST',body:JSON.stringify({kind,title,payload:{},max_attempts:3})}),'任务已进入本地队列')}
  async function recoveryAction(item:RecoveryItem,action:'RETRY'|'DISMISS'){if(action==='RETRY'&&!window.confirm('确认重新处理这个失败项？真实发送失败不会自动重发。'))return;await act(()=>api(`/recovery/${item.id}/action`,token,{method:'POST',body:JSON.stringify({action,confirmed:action==='RETRY'})}),action==='RETRY'?'已重新排队':'已从待处理箱移除')}
  async function addKnowledge(e:FormEvent){e.preventDefault();await act(()=>api('/knowledge',token,{method:'POST',body:JSON.stringify({title:knowledgeTitle,source_name:'后台手动录入',content:knowledgeContent,enabled:true})}),'知识已保存在本机');setKnowledgeTitle('');setKnowledgeContent('')}
  async function addTodo(e:FormEvent){e.preventDefault();await act(()=>api('/todos',token,{method:'POST',body:JSON.stringify({title:todoTitle,detail:'',priority:'NORMAL',due_at:todoDue?new Date(todoDue).toISOString():null,reminder_enabled:Boolean(todoDue),contact_id:todoContact||null})}),'待办已创建');setTodoTitle('');setTodoDue('');setTodoContact('')}
  async function patchTodo(item:TodoItem,status:string){await act(()=>api(`/todos/${item.id}`,token,{method:'PATCH',body:JSON.stringify({status})}),status==='DONE'?'待办已完成':'待办已恢复')}
  async function createDigest(){await act(()=>api('/digests/generate',token,{method:'POST'}),'摘要任务已排队，数秒后刷新即可查看')}
  async function lookupWeather(e:FormEvent){e.preventDefault();setBusy(true);try{setWeather(await api<WeatherTool>(`/tools/weather?location=${encodeURIComponent(weatherLocation)}`,token))}catch(x){notify(x instanceof Error?x.message:'天气查询失败')}finally{setBusy(false)}}
  async function searchOnline(e:FormEvent){e.preventDefault();setBusy(true);try{setSearchResult(await api<SearchTool>(`/tools/search?q=${encodeURIComponent(searchQuery)}&limit=5`,token))}catch(x){notify(x instanceof Error?x.message:'联网搜索失败')}finally{setBusy(false)}}
  async function addSticker(e:FormEvent){e.preventDefault();if(!stickerFile){notify('请先选择图片或动图');return}const bytes=new Uint8Array(await stickerFile.arrayBuffer());let binary='';for(let index=0;index<bytes.length;index+=0x8000)binary+=String.fromCharCode(...bytes.subarray(index,index+0x8000));await act(()=>api('/stickers',token,{method:'POST',body:JSON.stringify({label:stickerLabel,tags:stickerTags.split(/[,，\s]+/).filter(Boolean),file_name:stickerFile.name,mime_type:stickerFile.type,data_base64:btoa(binary),enabled:true,auto_reply_enabled:false})}),'贴图已加入本地资料库，自动回复默认关闭');setStickerLabel('');setStickerTags('');setStickerFile(null)}
  async function scanCache(){setBusy(true);try{const value=await api<StickerCacheScan>('/stickers/cache/scan',token,{method:'POST'});setCacheCandidates(value.candidates);setCacheRoots(value.roots);setSelectedCache([]);notify(value.candidates.length?`找到 ${value.candidates.length} 个最近的 QQ 表情候选`:'没有找到可导入的 QQ 表情缓存')}catch(x){notify(x instanceof Error?x.message:'扫描失败')}finally{setBusy(false)}}
  function toggleCache(id:string){setSelectedCache(value=>value.includes(id)?value.filter(item=>item!==id):value.length>=12?value:[...value,id])}
  async function importCache(){if(!selectedCache.length){notify('请先选择要导入的贴图');return}await act(()=>api('/stickers/cache/import',token,{method:'POST',body:JSON.stringify({items:selectedCache.map((candidateId,index)=>({candidate_id:candidateId,label:`QQ 缓存贴图 ${index+1}`,tags:[]}))})}),`已导入 ${selectedCache.length} 个贴图，自动回复仍保持关闭`);setSelectedCache([])}
  async function editStickerTags(item:StickerAsset){const value=window.prompt('输入触发词，用逗号分隔。只有回复正文命中触发词且“自动回复”已开启时，Neko 才会发送这张贴图。',item.tags.join(', '));if(value===null)return;const tags=value.split(/[,，\s]+/).map(tag=>tag.trim()).filter(Boolean);await act(()=>api(`/stickers/${item.id}`,token,{method:'PATCH',body:JSON.stringify({tags})}),tags.length?'触发词已保存':'触发词已清空，贴图不会被自动选中')}
  return <div className="page-stack workspace-center">
    <section className="workspace-hero panel"><div><p className="eyebrow">ASYNCHRONOUS WORK CENTER</p><h3>异步任务中心</h3><p>耗时工作在后台排队、失败可追踪；不会绕过原有发送门禁。</p></div><div className="workspace-stats"><span><b>{tasks.filter(item=>item.status==='PENDING'||item.status==='RUNNING').length}</b> 进行中</span><span><b>{recovery.filter(item=>item.status==='OPEN').length}</b> 待补偿</span><span><b>{todos.filter(item=>item.status==='OPEN').length}</b> 待办</span></div><div className="inline-actions"><button className="primary-button compact" disabled={busy} onClick={()=>void queue('RECOVERY_SYNC','扫描失败补偿项')}>扫描失败项</button><button className="secondary-button small" disabled={busy} onClick={()=>void load()}>刷新</button></div></section>
    <section className="panel online-tools"><div className="panel-heading"><div><p className="eyebrow">LIVE INFORMATION TOOLS</p><h3>天气、日历与联网搜索</h3></div><span className="safe-badge">北京时间 · 按需联网</span></div><div className="online-tool-grid">
      <article className="calendar-tool"><span>今天</span><strong>{calendar?.date??'读取中'}</strong><b>{calendar?.weekday??'—'}</b><p>{calendar?.upcoming.length?`未来有 ${calendar.upcoming.length} 项已设时间的待办`:'近期没有已设时间的待办'}</p>{calendar?.upcoming.slice(0,3).map(item=><small key={item.id}>{friendlyTime(item.due_at)} · {item.title}</small>)}</article>
      <article><strong>天气查询</strong><form className="tool-query" onSubmit={lookupWeather}><input value={weatherLocation} onChange={e=>setWeatherLocation(e.target.value)} placeholder="城市，例如：天津" required/><button className="secondary-button small" disabled={busy}>查询</button></form>{weather&&<div className="tool-result"><b>{weather.location} · {weather.current.weather}</b><p>{weather.current.temperature}℃，体感 {weather.current.apparent_temperature}℃ · 湿度 {weather.current.humidity}%</p><small>数据源：{weather.source} · 北京时间</small></div>}</article>
      <article><strong>联网搜索</strong><form className="tool-query" onSubmit={searchOnline}><input value={searchQuery} onChange={e=>setSearchQuery(e.target.value)} placeholder="输入需要查找的内容" required/><button className="secondary-button small" disabled={busy}>搜索</button></form>{searchResult&&<div className="search-results">{searchResult.results.map(item=><a key={item.url} href={item.url} target="_blank" rel="noreferrer"><b>{item.title}</b><span>{item.snippet||item.url}</span></a>)}</div>}</article>
    </div><p className="hint tool-note">聊天中明确询问天气、日期或要求联网搜索时，橙蓝也会按需调用这些工具；查不到时会说明失败，不会编造实时信息。</p></section>
    {routing&&<section className="panel routing-card"><div className="panel-heading"><div><p className="eyebrow">SMART MODEL ROUTING</p><h3>模型智能路由</h3></div><span className="safe-badge">自动判断 · 有序兜底</span></div><div className="routing-body"><div className="routing-rules">{routing.rules.map(item=><article key={item.task}><strong>{item.task}</strong><p>{item.detail}</p></article>)}</div><aside><span>当前模型链</span><p>{routing.providers.map(item=>`${item.name} / ${item.model}`).join(' → ')||'尚未启用模型'}</p><span>最近一次判断</span><strong>{routing.latest?.task_type??'暂无记录'}</strong><p>{routing.latest?.reason??'新消息到达后会记录路由理由。'}</p></aside></div></section>}
    <div className="workspace-grid">
      <section className="panel"><div className="panel-heading"><div><p className="eyebrow">TASK QUEUE</p><h3>后台任务</h3><span className="hint">最多保留 50 条，最新任务在上方</span></div><button className="secondary-button small" onClick={()=>void queue('KNOWLEDGE_REFRESH','刷新本地知识索引')}>刷新索引</button></div><div className="workspace-list bounded-feed">{tasks.slice(0,DYNAMIC_LIST_LIMIT).map(item=><article key={item.id}><div><strong>{item.title}</strong><p>{item.kind} · 第 {item.attempts}/{item.max_attempts} 次</p>{item.error_detail&&<small>{item.error_detail}</small>}</div><span className={`task-state ${item.status.toLowerCase()}`}>{item.status} {item.progress}%</span>{item.status==='FAILED'&&<button onClick={()=>void act(()=>api(`/tasks/${item.id}/retry`,token,{method:'POST'}),'任务已重试')}>重试</button>}</article>)}</div>{!tasks.length&&<EmptyState title="暂无后台任务" detail="生成摘要、刷新知识或补偿失败时会显示进度。"/>}</section>
      <section className="panel"><div className="panel-heading"><div><p className="eyebrow">RECOVERY BOX</p><h3>失败补偿箱</h3></div></div><div className="workspace-list bounded-feed">{recovery.filter(item=>item.status==='OPEN'||item.status==='RETRYING').slice(0,DYNAMIC_LIST_LIMIT).map(item=><article key={item.id}><div><strong>{item.kind}</strong><p>{item.source_type} · {item.error_code??'待检查'}</p><small>{item.error_detail}</small></div><div className="inline-actions"><button onClick={()=>void recoveryAction(item,'RETRY')}>安全重试</button><button onClick={()=>void recoveryAction(item,'DISMISS')}>忽略</button></div></article>)}</div>{!recovery.some(item=>item.status==='OPEN'||item.status==='RETRYING')&&<EmptyState title="没有待补偿失败" detail="入站和后台任务可安全重试；真实发送失败只允许人工核对。"/>}</section>
    </div>
    <div className="workspace-grid">
      <section className="panel"><div className="panel-heading"><div><p className="eyebrow">LOCAL KNOWLEDGE</p><h3>本地知识库</h3></div><span className="safe-badge">仅本机</span></div><form className="workspace-form" onSubmit={addKnowledge}><input value={knowledgeTitle} onChange={e=>setKnowledgeTitle(e.target.value)} placeholder="知识标题" required/><textarea value={knowledgeContent} onChange={e=>setKnowledgeContent(e.target.value)} placeholder="粘贴说明、资料或常用事实。命中当前问题时才会作为不可信参考资料提供给模型。" required/><button className="primary-button compact" disabled={busy}>保存知识</button></form><div className="workspace-list">{knowledge.map(item=><article key={item.id}><div><strong>{item.title}</strong><p>{item.content.slice(0,100)}{item.content.length>100?'…':''}</p><small>{item.source_name} · {item.sha256.slice(0,10)}</small></div><div className="inline-actions"><button onClick={()=>void act(()=>api(`/knowledge/${item.id}`,token,{method:'PATCH',body:JSON.stringify({enabled:!item.enabled})}),item.enabled?'知识已停用':'知识已启用')}>{item.enabled?'停用':'启用'}</button><button onClick={()=>void act(()=>api(`/knowledge/${item.id}`,token,{method:'DELETE'}),'知识已删除')}>删除</button></div></article>)}</div></section>
      <section className="panel"><div className="panel-heading"><div><p className="eyebrow">TODO & DAILY DIGEST</p><h3>待办与每日摘要</h3><span className="hint">联系人转交、手动待办统一管理</span></div><button className="secondary-button small" onClick={()=>void createDigest()}>生成今日摘要</button></div><form className="workspace-form compact-form" onSubmit={addTodo}><input value={todoTitle} onChange={e=>setTodoTitle(e.target.value)} placeholder="待办内容" required/><input type="datetime-local" value={todoDue} onChange={e=>setTodoDue(e.target.value)}/><select value={todoContact} onChange={e=>setTodoContact(e.target.value)}><option value="">不关联联系人</option>{contacts.map(item=><option key={item.id} value={item.id}>{item.display_name}</option>)}</select><button className="primary-button compact">添加待办</button></form><div className="workspace-list todo-list bounded-feed">{todos.slice(0,DYNAMIC_LIST_LIMIT).map(item=>{const linked=contacts.find(contact=>contact.id===item.contact_id);return <article key={item.id} className={item.kind==='CONTACT_RELAY'?'relay-todo':''}><button className={`todo-check ${item.status==='DONE'?'done':''}`} title={item.status==='DONE'?'恢复为未完成':'标记完成'} onClick={()=>void patchTodo(item,item.status==='DONE'?'OPEN':'DONE')}>{item.status==='DONE'?'✓':'○'}</button><div><div className="todo-title-line"><strong>{item.title}</strong>{item.kind==='CONTACT_RELAY'&&<span>联系人转交</span>}</div>{item.detail&&<p className="todo-detail">{item.detail}</p>}<p>{linked?`来自 ${linked.display_name} · `:''}{item.due_at?`北京时间 ${friendlyTime(item.due_at)}`:'未设截止时间'} · {item.kind==='CONTACT_RELAY'?item.delivery_status:item.priority}</p>{item.last_error&&<small>{item.last_error}</small>}</div></article>})}</div>{digests[0]&&<details className="digest-card"><summary>最近摘要 · {digests[0].local_date}</summary><pre>{digests[0].content}</pre></details>}</section>
    </div>
    <section className="panel">
      <div className="panel-heading"><div><p className="eyebrow">STICKERS</p><h3>表情包与贴图回复</h3></div><span className="warning-badge">自动发送默认关闭</span></div>
      <form className="sticker-form" onSubmit={addSticker}><input value={stickerLabel} onChange={e=>setStickerLabel(e.target.value)} placeholder="名称，例如：开心猫" required/><input value={stickerTags} onChange={e=>setStickerTags(e.target.value)} placeholder="触发标签，用逗号分隔，例如：开心,太好了"/><input type="file" accept="image/png,image/jpeg,image/gif,image/webp" onChange={e=>setStickerFile(e.target.files?.[0]??null)} required/><button className="primary-button compact">加入资料库</button></form>
      <div className="qq-cache-tools"><div><strong>从本机 QQ 表情缓存挑选</strong><p>只扫描 QQ 的 personal_emoji、marketface 和 emoji-recv 专用目录，不读取普通聊天图片。</p></div><button className="secondary-button small" disabled={busy} onClick={()=>void scanCache()}>扫描 QQ 缓存</button>{selectedCache.length>0&&<button className="primary-button compact" disabled={busy} onClick={()=>void importCache()}>导入已选 {selectedCache.length} 个</button>}</div>
      {cacheRoots.length>0&&<details className="cache-roots"><summary>查看已识别的 QQ 表情目录（{cacheRoots.length}）</summary>{cacheRoots.map(item=><code key={item}>{item}</code>)}</details>}
      {cacheCandidates.length>0&&<div className="cache-sticker-grid">{cacheCandidates.slice(0,60).map(item=><CacheStickerThumbnail key={item.id} item={item} token={token} selected={selectedCache.includes(item.id)} toggle={()=>toggleCache(item.id)}/>)}</div>}
      <div className="sticker-grid bounded-feed">{stickers.slice(0,DYNAMIC_LIST_LIMIT).map(item=><article key={item.id}><StickerThumbnail item={item} token={token}/><div><strong>{item.label}</strong><p>{item.tags.join(' · ')||'未设置触发标签'}</p><small>{item.source_kind==='QQ_CACHE'?'QQ 缓存导入':'手动添加'} · 已使用 {item.use_count} 次 · {item.mime_type}</small></div><label className="switch-line"><input type="checkbox" checked={item.auto_reply_enabled} onChange={()=>void act(()=>api(`/stickers/${item.id}`,token,{method:'PATCH',body:JSON.stringify({auto_reply_enabled:!item.auto_reply_enabled})}),item.auto_reply_enabled?'自动贴图已关闭':'自动贴图已开启')}/><span>自动回复</span></label><button onClick={()=>void editStickerTags(item)}>设置触发词</button><button onClick={()=>void act(()=>api(`/stickers/${item.id}`,token,{method:'DELETE'}),'贴图已从资料库移除')}>删除</button></article>)}</div>
    </section>
  </div>;
}

function MemoryPanel({contacts,token,notify}:{contacts:Contact[];token:string;notify:(s:string)=>void}) {
  const [contactId,setContactId]=useState(''); const [memories,setMemories]=useState<Memory[]>([]); const [kind,setKind]=useState('PREFERENCE'); const [content,setContent]=useState('');
  const selected=contacts.find(item=>item.id===contactId);
  const load=useCallback(async(id:string)=>{setContactId(id);try{setMemories(await api(`/contacts/${id}/memories`,token))}catch(x){notify(x instanceof Error?x.message:'读取失败')}},[token,notify]);
  async function add(e:FormEvent){e.preventDefault();try{await api(`/contacts/${contactId}/memories`,token,{method:'POST',body:JSON.stringify({kind,content})});setContent('');await load(contactId);notify('记忆只写入当前联系人空间')}catch(x){notify(x instanceof Error?x.message:'写入失败')}}
  async function remove(id:string){if(!window.confirm('确认删除这条联系人记忆？'))return;try{await api(`/memories/${id}`,token,{method:'DELETE'});await load(contactId);notify('记忆已删除')}catch(x){notify(x instanceof Error?x.message:'删除失败')}}
  async function edit(item:Memory){const next=window.prompt('编辑这条联系人记忆',item.content);if(next===null||next.trim()===item.content)return;try{await api(`/memories/${item.id}`,token,{method:'PATCH',body:JSON.stringify({kind:item.kind,content:next.trim()})});await load(contactId);notify('记忆已更新并立即用于后续对话')}catch(x){notify(x instanceof Error?x.message:'编辑失败')}}
  async function review(item:Memory,status:string){try{await api(`/memories/${item.id}/review`,token,{method:'PATCH',body:JSON.stringify({status,pinned:item.pinned,expires_at:item.expires_at??null})});await load(contactId);notify(status==='APPROVED'?'记忆已批准并可用于后续对话':'记忆已拒绝，不会进入模型上下文')}catch(x){notify(x instanceof Error?x.message:'审核失败')}}
  async function pin(item:Memory){try{await api(`/memories/${item.id}/review`,token,{method:'PATCH',body:JSON.stringify({status:item.review_status,pinned:!item.pinned,expires_at:item.expires_at??null})});await load(contactId);notify(item.pinned?'已取消置顶':'记忆已置顶')}catch(x){notify(x instanceof Error?x.message:'操作失败')}}
  return <div className="memory-layout"><aside className="panel"><div className="panel-heading"><h3>联系人空间</h3></div>{contacts.map(item=><button key={item.id} className={contactId===item.id?'selected':''} onClick={()=>void load(item.id)}><span className="contact-avatar">{item.display_name.slice(0,1)}</span><div><strong>{item.display_name}</strong><small>{item.memory_enabled?'记忆开启':'记忆关闭'}</small></div></button>)}</aside><section className="panel">{selected?<><div className="panel-heading"><div><p className="eyebrow">MEMORY CONTROL</p><h3>{selected.display_name} 的记忆管理中心</h3></div><span className={selected.memory_enabled?'safe-badge':'warning-badge'}>{selected.memory_enabled?'已启用':'已关闭'}</span></div><p className="hint">安全提取的记忆会直接生效，无需逐条审批；你仍可随时编辑、删除、置顶或停用历史条目。</p><form className="memory-form" onSubmit={add}><select value={kind} onChange={e=>setKind(e.target.value)}><option>PREFERENCE</option><option>PERSONAL_FACT</option><option>RELATIONSHIP_CONTEXT</option><option>ONGOING_TOPIC</option><option>CONVERSATION_CONVENTION</option></select><input value={content} onChange={e=>setContent(e.target.value)} placeholder="输入可长期记住的非敏感事实" required/><button className="primary-button compact" disabled={!selected.memory_enabled}>添加记忆</button></form><div className="memory-list">{memories.map(item=><article key={item.id} className={`memory-${item.review_status.toLowerCase()}`}><span>{item.kind} · {item.review_status}{item.pinned?' · 置顶':''}</span><p>{item.content}</p><small>{friendlyTime(item.created_at)}</small><div className="inline-actions">{item.review_status!=='APPROVED'&&<button onClick={()=>void review(item,'APPROVED')}>启用历史条目</button>}<button onClick={()=>void edit(item)}>编辑</button><button onClick={()=>void pin(item)}>{item.pinned?'取消置顶':'置顶'}</button><button onClick={()=>void remove(item.id)}>删除</button></div></article>)}</div>{!memories.length&&<EmptyState title="还没有长期记忆" detail="密码、API Key、令牌、私钥、银行卡与验证码会被程序拒绝。"/>}</>:<EmptyState title="选择联系人" detail="每位联系人拥有完全隔离的记忆空间。"/>}</section></div>;
}

function UsagePanel({usage}:{usage:Usage}) {
  const peak=Math.max(1,...usage.last_7_days.map(item=>item.total_tokens));
  return <div className="page-stack"><section className="usage-overview"><article><span>今日模型回复</span><strong>{usage.today.messages}</strong><small>条已生成消息</small></article><article><span>输入 Token</span><strong>{usage.today.input_tokens.toLocaleString()}</strong><small>今日累计</small></article><article><span>输出 Token</span><strong>{usage.today.output_tokens.toLocaleString()}</strong><small>今日累计</small></article><article><span>总 Token</span><strong>{usage.today.total_tokens.toLocaleString()}</strong><small>今日累计</small></article></section><section className="panel"><div className="panel-heading"><div><p className="eyebrow">LAST 7 DAYS</p><h3>模型用量趋势</h3></div><span className="hint">仅统计本机已落库 AI 消息</span></div><div className="usage-chart" role="img" aria-label="最近七天 Token 用量柱状图">{usage.last_7_days.map(item=><div key={item.date}><span>{item.total_tokens.toLocaleString()}</span><i><b style={{height:`${Math.max(3,(item.total_tokens/peak)*100)}%`}}/></i><small>{item.date.slice(5)}</small></div>)}</div></section><section className="panel table-panel"><div className="panel-heading"><div><p className="eyebrow">PROVIDER BREAKDOWN</p><h3>模型明细</h3></div></div>{usage.by_model.length?<div className="usage-table"><div className="usage-row head"><span>Provider / 模型</span><span>消息</span><span>输入</span><span>输出</span><span>总计</span></div>{usage.by_model.map(item=><div className="usage-row" key={`${item.provider}-${item.model}`}><span><b>{item.provider}</b><small>{item.model}</small></span><span>{item.messages}</span><span>{item.input_tokens.toLocaleString()}</span><span>{item.output_tokens.toLocaleString()}</span><strong>{item.total_tokens.toLocaleString()}</strong></div>)}</div>:<EmptyState title="尚无模型用量" detail="完成一次安全演练或真实模型回复后，这里会按 Provider 与模型汇总。"/>}</section></div>;
}

function LogsPanel({logs,incidents,token,refresh,notify}:{logs:Log[];incidents:Incident[];token:string;refresh:()=>Promise<void>;notify:(s:string)=>void}) {
  async function resolve(id:string){try{await api(`/incidents/${id}/resolve`,token,{method:'POST'});await refresh();notify('风险事件已标记处理')}catch(x){notify(x instanceof Error?x.message:'操作失败')}}
  return <div className="page-stack"><section className="panel"><div className="panel-heading"><div><p className="eyebrow">INCIDENTS</p><h3>未处理风险事件</h3></div><span className="warning-badge">{incidents.length} 项</span></div>{incidents.length?<div className="incident-table">{incidents.map(item=><article key={item.id}><span className="warning-dot"/><div><strong>{item.title}</strong><p>{item.detail}</p><small>{item.kind} · {friendlyTime(item.created_at)}</small></div><button className="secondary-button small" onClick={()=>void resolve(item.id)}>标记已处理</button></article>)}</div>:<EmptyState title="没有未处理事件" detail="风险事件、模型故障和连接器失败会显示在这里。"/>}</section><section className="panel table-panel"><div className="panel-heading"><div><p className="eyebrow">7 DAY AUDIT TRAIL</p><h3>操作审计日志</h3></div><span className="hint">日志不记录完整 API Key</span></div><div className="log-list"><div className="log-row head"><span>时间</span><span>事件</span><span>等级</span><span>安全详情</span></div>{logs.map(item=><div className="log-row" key={item.id}><span>{friendlyTime(item.created_at)}</span><strong>{item.event}</strong><span className={`level ${item.level.toLowerCase()}`}>{item.level}</span><code>{JSON.stringify(item.detail)}</code></div>)}</div></section></div>;
}

function SettingsPanel({runtime,contacts,acceptance,readiness,token,refresh,notify}:{runtime:Runtime;contacts:Contact[];acceptance:QQAcceptance;readiness:Readiness;token:string;refresh:()=>Promise<void>;notify:(s:string)=>void}) {
  async function accept(stage:string){if(!window.confirm(`确认已审阅并完成 ${stage} 阶段验收？`))return;try{await api(`/runtime/accept/${stage}`,token,{method:'POST',body:JSON.stringify({confirmed:true})});await refresh();notify(`${stage} 阶段已确认`)}catch(x){notify(x instanceof Error?x.message:'确认失败')}}
  async function gate(value:string){if(value==='LIVE'&&!window.confirm('LIVE 会允许受支持真实通道发送消息。确认已经从单个测试联系人开始，并理解平台风险？'))return;try{await api('/runtime/release-gate',token,{method:'PUT',body:JSON.stringify({gate:value})});await refresh();notify(`发布门禁已切换到 ${value}`)}catch(x){notify(x instanceof Error?x.message:'切换失败')}}
  const eligible=contacts.filter(item=>item.platform==='QQ'&&item.whitelisted&&item.ai_enabled&&item.importance!=='MANUAL_ONLY');
  async function startAcceptance(){if(eligible.length!==1){notify('请先只保留 1 个启用 AI 的 QQ 白名单测试联系人');return}if(!window.confirm('开始真实 QQ 200 条验收？请保持自然消息节奏，不要连续轰炸。期间还需开启急停并发送 1 条测试消息，确认它被阻止。'))return;try{await api('/acceptance/qq/start',token,{method:'POST',body:JSON.stringify({contact_id:eligible[0].id})});await refresh();notify('QQ 200 条真实验收已开始')}catch(x){notify(x instanceof Error?x.message:'无法开始验收')}}
  async function cancelAcceptance(){if(!acceptance.id||!window.confirm('确认取消当前 QQ 验收？已记录证据会保留。'))return;try{await api(`/acceptance/qq/${acceptance.id}/cancel`,token,{method:'POST'});await refresh();notify('QQ 验收已取消')}catch(x){notify(x instanceof Error?x.message:'取消失败')}}
  const progress=Math.min(100,((acceptance.unique_received??0)/(acceptance.expected_messages??200))*100);
  return <div className="page-stack">
    <section className="release-flow">
      <article className="done"><span>01</span><div><p className="eyebrow">CURRENT SAFE DEFAULT</p><h3>SIMULATION</h3><p>所有消息只进入隔离模拟连接器，不触达真实联系人。</p></div><button className="secondary-button" onClick={()=>void accept('SIMULATION')}>{runtime.simulation_accepted?'已验收':'确认验收'}</button></article>
      <i/>
      <article className={runtime.simulation_accepted?'ready':''}><span>02</span><div><p className="eyebrow">OBSERVE WITHOUT SENDING</p><h3>SHADOW</h3><p>真实接收、模型生成，但回复仅作为影子结果记录。</p></div><button className="secondary-button" disabled={!runtime.simulation_accepted} onClick={()=>void accept('SHADOW')}>{runtime.shadow_accepted?'已验收':'确认验收'}</button></article>
      <i/>
      <article className={runtime.shadow_accepted?'ready':''}><span>03</span><div><p className="eyebrow">CONTROLLED CHANNEL ONLY</p><h3>LIVE</h3><p>只允许一个已验证通道：QQ 官方，或明确承担风险的 NapCat。AutoWx 永远只生成草稿。</p></div></article>
    </section>
    <section className="panel readiness-panel">
      <div className="panel-heading"><div><p className="eyebrow">EXTERNAL ACCEPTANCE READINESS</p><h3>完全通过还差什么</h3></div><span className={readiness.ready?'safe-badge':'warning-badge'}>{readiness.ready?'全部就绪':'等待外部环境'}</span></div>
      <div className="readiness-grid">{readiness.checks.map(item=><article key={item.id} className={item.ready?'ready':''}><span>{item.ready?'✓':'·'}</span><div><strong>{item.label}</strong><p>{item.detail}</p></div></article>)}</div>
    </section>
    <section className="panel gate-control"><div><p className="eyebrow">RELEASE GATE</p><h3>当前发布环境</h3><p>门禁只能逐级前进；LIVE 要求恰好一个已验证 QQ 通道和一个测试联系人。当前 LIVE 时段（北京时间）：{runtime.live_time_window_enabled?`${runtime.live_auto_start}–${runtime.live_auto_end}`:'时间门禁已关闭'}。</p></div><div className="segmented large">{['SIMULATION','SHADOW','LIVE'].map(item=><button key={item} className={runtime.release_gate===item?'selected':''} onClick={()=>void gate(item)}>{item}</button>)}</div></section>
    <section className="panel acceptance-panel">
      <div className="panel-heading"><div><p className="eyebrow">REAL CHANNEL EVIDENCE</p><h3>QQ 200 条端到端验收</h3></div><span className={`acceptance-state ${acceptance.status.toLowerCase()}`}>{acceptance.status}</span></div>
      {acceptance.status==='NOT_STARTED'?<div className="acceptance-empty"><p>绑定唯一 QQ 测试联系人后，系统会自动证明唯一入站、重复投递去重、零错收件人和急停有效。安全频控始终保留，验收不会放宽每分钟上限。</p><button className="primary-button" disabled={runtime.release_gate!=='LIVE'||eligible.length!==1} onClick={()=>void startAcceptance()}>开始真实验收</button></div>:<><div className="acceptance-target"><div><span>测试联系人</span><strong>{acceptance.target_name}</strong></div><div><span>开始时间</span><strong>{acceptance.started_at?friendlyTime(acceptance.started_at):'—'}</strong></div></div><div className="acceptance-progress"><div><span style={{width:`${progress}%`}}/></div><p>{acceptance.unique_received??0} / {acceptance.expected_messages??200} 条唯一真实入站 · 剩余 {acceptance.remaining_messages??200}</p></div><div className="acceptance-metrics"><article><span>系统重复发送</span><strong>{acceptance.duplicate_sends??0}</strong></article><article><span>错误联系人发送</span><strong>{acceptance.wrong_recipient_sends??0}</strong></article><article><span>急停违规</span><strong>{acceptance.kill_switch_violations??0}</strong></article><article><span>急停演练</span><strong>{acceptance.kill_switch_tested?'已证明':'待执行'}</strong></article><article><span>平台重复投递</span><strong>{acceptance.duplicate_webhooks_safely_ignored??0}</strong></article><article><span>平台/连接故障</span><strong>{acceptance.connector_or_platform_failures??0}</strong></article></div>{acceptance.status==='RUNNING'&&<div className="acceptance-actions"><p>请按自然节奏继续测试；至少一次开启“紧急停止”后再发 1 条消息，确认系统只记录、不回复，然后解除急停。</p><button className="secondary-button" onClick={()=>void refresh()}>刷新证据</button><button className="ghost-button danger" onClick={()=>void cancelAcceptance()}>取消验收</button></div>}{acceptance.status==='PASSED'&&<div className="acceptance-verdict pass"><strong>真实 QQ 核心指标通过</strong><p>200 条唯一入站和急停演练已有本地证据，系统违规为 0。</p></div>}{acceptance.status==='FAILED'&&<div className="acceptance-verdict fail"><strong>验收未通过，LIVE 应立即停止</strong><p>检测到 {acceptance.system_violations??0} 项系统违规，请先查看日志与风险事件。</p></div>}</>}
    </section>
    <section className="callout warning"><strong>“避免封号”的正式边界</strong><p>系统只能降低由自身错误造成的异常爆发发送、批量骚扰与策略绕过风险，不能承诺平台账号永不受处罚。禁止 Hook、协议逆向、设备指纹修改与反检测。</p></section>
  </div>;
}

function Switch({checked,label,onChange,disabled=false}:{checked:boolean;label:string;onChange:(value:boolean)=>void;disabled?:boolean}) {return <button type="button" role="switch" aria-checked={checked} aria-label={label} className={`switch ${checked?'on':''}`} disabled={disabled} onClick={()=>onChange(!checked)}><span/></button>}
function EmptyState({title,detail}:{title:string;detail:string}) {return <div className="empty-state"><span>·</span><h3>{title}</h3><p>{detail}</p></div>}

