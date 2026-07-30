import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { AgentModel, ChatMessage, ChatSession, ProviderBinding } from '../types';
import { VexaAudioAnalyser } from './vexaAudioAnalyser';
import { VEXA_COPY } from './vexa/vexaCopy';
import type { CoreHudData, DashboardRoute, GlobalSystemState } from './vexa/vexaDashboardTypes';
import { useVexaTelemetry } from './vexa/useVexaTelemetry';
import { VexaTopHeader, type SidePanel } from './vexa/VexaTopHeader';
import { ChatHistoryCard, ModelSelectorCard, NeuralDensityCard, SystemStatusCard } from './vexa/VexaSystemSidebar';
import { ActiveProtocolsCard, DataStreamCard, NeuralActivityCard, SystemResourcesCard } from './vexa/VexaMetricsSidebar';
import { AgentCircuitCard, ConversationCard, SystemInsightsCard } from './vexa/VexaRightSidebar';
import { VexaCoreStage, type MicrophoneState } from './vexa/VexaCoreStage';
import { VexaBottomNav } from './vexa/VexaBottomNav';
import { VexaConfirmationDrawer, VexaEmergencyOverlay } from './vexa/VexaConfirmationDrawer';

/** Consecutive unheard turns dialog mode retries before it stops listening. */
const MAX_IDLE_RETRIES = 3;

/** Product version shown bottom-right; sourced from the build, not hard-coded per screen. */
const APP_VERSION = '2.0.1';

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('jarvis_auth_token');
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

export type VoicePhase = 'offline' | 'ready' | 'listening' | 'transcribing' | 'thinking' | 'speaking' | 'error';

interface TtsStatus {
  enabled: boolean;
  available: boolean;
  active_provider?: string | null;
  voice?: string | null;
  browser_fallback: boolean;
}

interface SttStatus {
  enabled: boolean;
  dependency_available: boolean;
  model: string;
  loaded: boolean;
  loading?: boolean;
  last_error?: string | null;
}

interface VexaCommandCenterProps {
  agents: AgentModel[];
  messages: ChatMessage[];
  isConnected: boolean;
  isGenerating: boolean;
  isSpeaking: boolean;
  micState: 'off' | 'listening' | 'capturing' | 'transcribing' | 'error';
  micErrorMessage?: string;
  /** Increments whenever a listening turn ended with nothing recognised — dialog mode re-arms on it. */
  voiceIdleTick?: number;
  onVoiceToggle: () => void;
  onCommand: (text: string) => boolean;
  onStop: () => void;
  language: 'ru' | 'en';
  /** Live microphone stream while capturing — used to drive the core animation with real voice amplitude. */
  micStreamRef: React.RefObject<MediaStream | null>;
  /** Currently playing TTS audio element — used to drive the core animation while Vexa speaks. */
  ttsAudioElRef: React.RefObject<HTMLAudioElement | null>;
  /** Opens the floating chat window; pass an agent id to switch to that agent's session first. */
  onOpenAgentChat: (agentId?: string) => void;
  chatSessions: ChatSession[];
  currentChatId: string;
  getSessionLabel: (sessionId: string) => string;
  onCreateSession: () => void;
  /** Refreshes the agents list after the quick local/external model switch saves. */
  fetchAgents?: () => void;
  /** Switches the main workspace to the minimal chat view. */
  onSwitchToSimpleMode?: () => void;
  /** Routes the bottom navigation / quick actions onto App.tsx's workspace tabs. */
  onNavigate?: (route: DashboardRoute) => void;
}

export function phaseFor(
  isConnected: boolean,
  micState: VexaCommandCenterProps['micState'],
  isGenerating: boolean,
  isSpeaking: boolean,
): VoicePhase {
  if (!isConnected) return 'offline';
  if (micState === 'error') return 'error';
  if (micState === 'capturing' || micState === 'listening') return 'listening';
  if (micState === 'transcribing') return 'transcribing';
  if (isSpeaking) return 'speaking';
  if (isGenerating) return 'thinking';
  return 'ready';
}

function micrphoneStateFor(
  micState: VexaCommandCenterProps['micState'],
  secureContext: boolean,
  sttReady: boolean,
): MicrophoneState {
  if (!secureContext) return 'unavailable';
  if (micState === 'error') return 'error';
  if (micState === 'transcribing') return 'processing';
  if (micState === 'capturing' || micState === 'listening') return 'listening';
  return sttReady ? 'ready' : 'muted';
}

export function VexaCommandCenter({
  agents,
  messages,
  isConnected,
  isGenerating,
  isSpeaking,
  micState,
  micErrorMessage,
  voiceIdleTick = 0,
  onVoiceToggle,
  onCommand,
  onStop,
  language,
  micStreamRef,
  ttsAudioElRef,
  onOpenAgentChat,
  chatSessions,
  currentChatId,
  getSessionLabel,
  onCreateSession,
  fetchAgents,
  onSwitchToSimpleMode,
  onNavigate,
}: VexaCommandCenterProps) {
  const copy = VEXA_COPY[language];
  const phase = phaseFor(isConnected, micState, isGenerating, isSpeaking);
  const secureMicrophone = typeof window !== 'undefined' && window.isSecureContext;

  // ---- local UI state -------------------------------------------------------
  const [draft, setDraft] = useState(() => localStorage.getItem('hermes_vexa_draft') || '');
  const [sendError, setSendError] = useState<string | null>(null);
  const [conversationMode, setConversationMode] = useState(false);
  const [confirmationsOpen, setConfirmationsOpen] = useState(false);
  /** Only meaningful below 1280px, where the side columns render as drawers. */
  const [openPanel, setOpenPanel] = useState<SidePanel | null>(null);
  const [simpleGraphics, setSimpleGraphics] = useState(
    () => localStorage.getItem('hermes_vexa_simple_graphics') === '1',
  );
  const [webglAvailable, setWebglAvailable] = useState(true);
  const [ttsStatus, setTtsStatus] = useState<TtsStatus | null>(null);
  const [sttStatus, setSttStatus] = useState<SttStatus | null>(null);
  const [providers, setProviders] = useState<ProviderBinding[]>([]);
  const [pendingExternalId, setPendingExternalId] = useState<string | null>(null);
  const [pendingModelName, setPendingModelName] = useState('');
  const [jarvisSaving, setJarvisSaving] = useState(false);

  const [audioAnalyser] = useState(() => new VexaAudioAnalyser(micStreamRef, ttsAudioElRef));
  useEffect(() => () => audioAnalyser.dispose(), [audioAnalyser]);

  useEffect(() => {
    localStorage.setItem('hermes_vexa_simple_graphics', simpleGraphics ? '1' : '0');
  }, [simpleGraphics]);

  // Draft survives a reload or an accidental tab switch.
  useEffect(() => {
    const timer = window.setTimeout(() => localStorage.setItem('hermes_vexa_draft', draft), 400);
    return () => window.clearTimeout(timer);
  }, [draft]);

  // ---- derived agent/voice facts -------------------------------------------
  const jarvisAgent = useMemo(() => agents.find(agent => agent.id === 'jarvis'), [agents]);
  const [initialLocalModel] = useState(() => jarvisAgent?.model || 'qwen3:8b');
  const activeProviders = useMemo(() => providers.filter(item => item.status === 'active'), [providers]);
  const effectiveProviderValue = jarvisAgent && activeProviders.some(item => item.id === jarvisAgent.model_provider)
    ? jarvisAgent.model_provider!
    : 'ollama';
  const activeProviderName = activeProviders.find(item => item.id === effectiveProviderValue)?.name;

  // Outcomes of the assistant runs present in the loaded history — the only accuracy
  // signal available on the client, since the backend publishes none.
  const runOutcomes = useMemo(() => {
    let completed = 0;
    let failed = 0;
    for (const message of messages) {
      if (message.role !== 'assistant' || message.streaming) continue;
      const status = String(message.meta?.status ?? '').toLowerCase();
      if (status === 'error' || status === 'failed' || status === 'cancelled') failed += 1;
      else completed += 1;
    }
    return { completed, failed };
  }, [messages]);

  const activeAgents = useMemo(
    () => agents.filter(agent => ['working', 'running', 'active', 'processing'].includes(String(agent.status || '').toLowerCase())),
    [agents],
  );

  const sttReady = Boolean(sttStatus?.enabled && sttStatus?.dependency_available) && secureMicrophone;
  const voiceEngine = ttsStatus?.active_provider || 'TTS';
  const voiceName = ttsStatus?.voice || (ttsStatus?.available ? voiceEngine : copy.browser);

  // ---- telemetry ------------------------------------------------------------
  const telemetry = useVexaTelemetry({
    enabled: true,
    runOutcomes,
    isConnected,
    agentsTotal: agents.length,
    agentsActive: activeAgents.length,
    isGenerating,
    isSpeaking,
    isListening: micState === 'capturing' || micState === 'listening',
    voiceEngine,
    voiceName,
    voiceAvailable: Boolean(ttsStatus?.available),
    sttReady,
  });

  const { emergencyStopped } = telemetry;

  // ---- remote status --------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      fetch('/api/voice/tts/status').then(response => response.ok ? response.json() : Promise.reject(new Error('tts'))),
      fetch('/api/voice/status').then(response => response.ok ? response.json() : Promise.reject(new Error('stt'))),
    ]).then(([tts, stt]) => {
      if (cancelled) return;
      setTtsStatus(tts.status === 'fulfilled' ? tts.value : null);
      setSttStatus(stt.status === 'fulfilled' ? stt.value : null);
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/providers', { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : []))
      .then(data => { if (!cancelled) setProviders(Array.isArray(data) ? data : []); })
      .catch(() => { if (!cancelled) setProviders([]); });
    return () => { cancelled = true; };
  }, []);

  // ---- model switching ------------------------------------------------------
  const saveJarvisProvider = async (nextProviderId: string, nextModel: string) => {
    if (!jarvisAgent || !nextModel.trim()) return;
    setJarvisSaving(true);
    try {
      const response = await fetch('/api/agents', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({
          ...jarvisAgent,
          model_provider: nextProviderId,
          model_type: nextProviderId === 'ollama' ? 'local' : 'external',
          model: nextModel.trim(),
        }),
      });
      if (response.ok) {
        fetchAgents?.();
        setPendingExternalId(null);
        setPendingModelName('');
      }
    } finally {
      setJarvisSaving(false);
    }
  };

  const handleProviderSelect = (value: string) => {
    if (value === effectiveProviderValue) {
      setPendingExternalId(null);
      return;
    }
    // Switching mid-generation would abandon the in-flight answer — make that explicit
    // instead of silently dropping it.
    if (isGenerating && !window.confirm(copy.modelSwitchBusy)) return;
    if (isGenerating) onStop();
    if (value === 'ollama') {
      void saveJarvisProvider('ollama', initialLocalModel);
    } else {
      setPendingExternalId(value);
      setPendingModelName('');
    }
  };

  // ---- dialog mode ----------------------------------------------------------
  const conversationKey = useMemo(() => {
    const assistant = [...messages].reverse().find(message => message.role === 'assistant' && !message.streaming);
    return assistant ? `${assistant.id || ''}:${assistant.run_id || ''}:${assistant.content.length}` : '';
  }, [messages]);

  const lastAutoListenRef = useRef('');
  const lastIdleTickRef = useRef(voiceIdleTick);
  const idleRetriesRef = useRef(0);

  useEffect(() => {
    if (!conversationMode || !conversationKey || conversationKey === lastAutoListenRef.current) return;
    if (!isConnected || isGenerating || isSpeaking || micState !== 'off') return;
    const answerKey = conversationKey;
    // Only stamp lastAutoListenRef once the mic actually restarts. speakText() fetches
    // TTS audio asynchronously, so `isSpeaking` can still be false for a brief window
    // right after the answer arrives — if we stamped the ref here (before the timeout
    // fires) and isSpeaking then flips true (cancelling this timer via cleanup, correctly
    // deferring the relisten), the ref would already mark this answer as "handled" and
    // the effect would never reschedule once isSpeaking goes false again for real.
    // 650ms wasn't enough of a buffer past isSpeaking going false — the physical
    // reverb/echo tail in the room can still be audible after audio.onended fires,
    // and re-arming the mic into that tail both sounds distorted and can trip the
    // STT's own speech/silence detection on the next turn.
    const timer = window.setTimeout(() => {
      lastAutoListenRef.current = answerKey;
      idleRetriesRef.current = 0;
      onVoiceToggle();
    }, 1100);
    return () => window.clearTimeout(timer);
  }, [conversationKey, conversationMode, isConnected, isGenerating, isSpeaking, micState, onVoiceToggle]);

  // A turn where nothing was recognised produces no answer, and the effect above
  // only re-arms on a new answer — so a single unheard phrase used to end the
  // conversation silently, mid-dialog. Re-arm on those turns too, giving up after
  // MAX_IDLE_RETRIES in a row so a noisy room can't cycle the microphone forever.
  useEffect(() => {
    if (voiceIdleTick === lastIdleTickRef.current) return;
    if (!conversationMode) {
      lastIdleTickRef.current = voiceIdleTick;
      return;
    }
    // Not consuming the tick here: if the mic is still winding down, this effect
    // re-runs on the next state change and picks the retry up then.
    if (!isConnected || isGenerating || isSpeaking || micState !== 'off') return;

    const tick = voiceIdleTick;
    const retries = idleRetriesRef.current + 1;
    if (retries > MAX_IDLE_RETRIES) {
      lastIdleTickRef.current = tick;
      idleRetriesRef.current = 0;
      setConversationMode(false);
      return;
    }
    const timer = window.setTimeout(() => {
      lastIdleTickRef.current = tick;
      idleRetriesRef.current = retries;
      onVoiceToggle();
    }, 400);
    return () => window.clearTimeout(timer);
  }, [voiceIdleTick, conversationMode, isConnected, isGenerating, isSpeaking, micState, onVoiceToggle]);

  const toggleConversation = () => {
    if (!secureMicrophone) {
      onVoiceToggle();
      return;
    }
    const next = !conversationMode;
    setConversationMode(next);
    lastAutoListenRef.current = conversationKey;
    idleRetriesRef.current = 0;
    if (next && micState === 'off' && !isGenerating && !isSpeaking) onVoiceToggle();
  };

  // ---- command submission ---------------------------------------------------
  const sendingRef = useRef(false);

  const submitCommand = useCallback((text: string): boolean => {
    const command = text.trim();
    if (!command || !isConnected || isGenerating || emergencyStopped) return false;
    if (sendingRef.current) return false;
    sendingRef.current = true;
    try {
      const accepted = onCommand(command);
      setSendError(accepted ? null : copy.sendFailed);
      return accepted;
    } finally {
      // Released on the next tick so a double Enter cannot post twice, while a rejected
      // command still leaves the composer usable immediately.
      window.setTimeout(() => { sendingRef.current = false; }, 0);
    }
  }, [copy.sendFailed, emergencyStopped, isConnected, isGenerating, onCommand]);

  const submitDraft = () => {
    if (submitCommand(draft)) setDraft('');
  };

  // ---- emergency stop / approvals ------------------------------------------
  const controlAction = useCallback(async (path: string, body: Record<string, unknown>) => {
    try {
      await fetch(path, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) });
    } catch {
      /* surfaced by the Processes tab; the drawer stays open so the user can retry */
    }
    await telemetry.refreshControl();
  }, [telemetry]);

  const handleEmergencyStop = useCallback(() => {
    if (micState !== 'off') onVoiceToggle();
    if (isGenerating) onStop();
    void controlAction('/api/control-plane/kill', {
      reason: 'manual_user_request',
      source: 'dashboard',
      idempotencyKey: crypto.randomUUID?.() ?? `${Date.now()}`,
    });
  }, [controlAction, isGenerating, micState, onStop, onVoiceToggle]);

  const handleResume = useCallback(() => {
    if (!window.confirm(copy.resumeConfirm)) return;
    void controlAction('/api/control-plane/resume', { reason: 'Resumed from the Vexa dashboard' });
  }, [controlAction, copy.resumeConfirm]);

  const handleApprove = useCallback((id: string) => {
    void controlAction(`/api/control-plane/tasks/${encodeURIComponent(id)}/approve`, { actor: 'owner:web' });
  }, [controlAction]);

  const handleReject = useCallback((id: string) => {
    void controlAction(`/api/control-plane/tasks/${encodeURIComponent(id)}/reject`, { actor: 'owner:web' });
  }, [controlAction]);

  // ---- presentation ---------------------------------------------------------
  const runtimeLabel = emergencyStopped ? copy.emergencyStopped
    : telemetry.confirmations.length > 0 && !isGenerating ? copy.awaitingConfirmation
      : phase === 'ready' ? copy.ready
        : phase === 'listening' ? copy.listening
          : phase === 'transcribing' ? copy.transcribing
            : phase === 'thinking' ? copy.thinking
              : phase === 'speaking' ? copy.speaking
                : phase === 'offline' ? copy.offline
                  : copy.error;

  const runtimeTone: 'normal' | 'warn' | 'error' = emergencyStopped || phase === 'error' || phase === 'offline'
    ? 'error'
    : telemetry.confirmations.length > 0 ? 'warn' : 'normal';

  const globalState: GlobalSystemState = emergencyStopped ? 'emergency_stopped'
    : !isConnected ? 'offline'
      : telemetry.overview.stale ? 'degraded' : 'online';

  const hud: CoreHudData = {
    processLabel: phase === 'listening' ? 'VOICE INPUT' : copy.hudDataPatterns,
    coreStatus: emergencyStopped ? 'HALTED' : isConnected ? 'ACTIVE' : 'OFFLINE',
    linkStrength: telemetry.neuralDensity,
    thoughtFlow: isGenerating ? 'streaming' : isSpeaking ? 'processing' : micState !== 'off' ? 'receiving' : 'idle',
  };

  const resourceRows = [
    { id: 'cpu', label: 'CPU', value: telemetry.overview.resources.cpu },
    { id: 'ram', label: 'RAM', value: telemetry.overview.resources.memory },
    { id: 'gpu', label: 'GPU', value: telemetry.overview.resources.gpu },
    { id: 'network', label: 'NETWORK', value: telemetry.overview.resources.network },
  ];

  const navigate = (route: DashboardRoute) => onNavigate?.(route);

  return (
    <div className="vx-shell">
      <div className="vx-background" aria-hidden="true" />
      <div className="vx-noise" aria-hidden="true" />

      <div className={`vx-dashboard${openPanel ? ` show-${openPanel}` : ''}`}>
        <div className="vx-area-header">
          <VexaTopHeader
            copy={copy}
            state={globalState}
            stale={telemetry.overview.stale}
            runtimeLabel={runtimeLabel}
            runtimeTone={runtimeTone}
            pendingConfirmations={telemetry.confirmations.length}
            onSwitchToSimpleView={onSwitchToSimpleMode}
            onOpenAnalytics={() => navigate('analytics')}
            onOpenAgents={() => navigate('agents')}
            onOpenProcesses={() => navigate('protocols')}
            onOpenConfirmations={() => setConfirmationsOpen(true)}
            onOpenSettings={() => navigate('settings')}
            openPanel={openPanel}
            onTogglePanel={panel => setOpenPanel(current => current === panel ? null : panel)}
          />
        </div>

        <div className="vx-area-system vx-column">
          <SystemStatusCard copy={copy} overview={telemetry.overview} />
          {jarvisAgent && (
            <ModelSelectorCard
              copy={copy}
              providers={activeProviders}
              selectedProviderId={effectiveProviderValue}
              pendingProviderId={pendingExternalId}
              pendingModelName={pendingModelName}
              saving={jarvisSaving}
              activeProviderName={activeProviderName}
              onSelectProvider={handleProviderSelect}
              onPendingModelChange={setPendingModelName}
              onApply={() => pendingExternalId && void saveJarvisProvider(pendingExternalId, pendingModelName)}
              onCancel={() => { setPendingExternalId(null); setPendingModelName(''); }}
            />
          )}
          <ChatHistoryCard
            copy={copy}
            sessions={chatSessions}
            currentChatId={currentChatId}
            getSessionLabel={getSessionLabel}
            onCreate={onCreateSession}
            onSelect={sessionId => onOpenAgentChat(sessionId)}
            onSeeAll={() => onOpenAgentChat()}
          />
          <NeuralDensityCard copy={copy} density={telemetry.neuralDensity} />
        </div>

        <div className="vx-area-metrics vx-column">
          <SystemResourcesCard copy={copy} rows={resourceRows} />
          <NeuralActivityCard copy={copy} series={telemetry.activitySeries} live={isConnected} />
          <DataStreamCard copy={copy} rows={telemetry.dataStream} onSeeAll={() => navigate('analytics')} />
          <ActiveProtocolsCard copy={copy} protocols={telemetry.protocols} />
        </div>

        <div className="vx-area-core">
          <VexaCoreStage
            copy={copy}
            phase={phase}
            audioAnalyser={audioAnalyser}
            pulseKey={conversationKey}
            simpleMode={simpleGraphics || !webglAvailable}
            hud={hud}
            micState={micrphoneStateFor(micState, secureMicrophone, sttReady)}
            micErrorMessage={micErrorMessage || (!secureMicrophone ? copy.secureContextRequired : undefined)}
            conversationMode={conversationMode}
            onToggleConversation={toggleConversation}
            onVoiceToggle={onVoiceToggle}
            riskEnabled
            pendingConfirmations={telemetry.confirmations.length}
            emergencyStopped={emergencyStopped}
            onOpenRiskControl={() => setConfirmationsOpen(true)}
            isConnected={isConnected}
            isGenerating={isGenerating}
            draft={draft}
            onDraftChange={value => { setDraft(value); if (sendError) setSendError(null); }}
            onSubmit={submitDraft}
            onStop={onStop}
            sendError={sendError}
            onWebGLFallback={available => setWebglAvailable(available)}
          />
        </div>

        <div className="vx-area-nav">
          <VexaBottomNav
            copy={copy}
            active="terminal"
            onNavigate={navigate}
            version={APP_VERSION}
            connected={isConnected}
          />
        </div>

        <div className="vx-area-right vx-column">
          <AgentCircuitCard
            copy={copy}
            agents={agents}
            activeCount={activeAgents.length}
            selectedAgentId="jarvis"
            onOpenAgent={agentId => onOpenAgentChat(agentId)}
            onOpenAll={() => onOpenAgentChat()}
          />
          <ConversationCard
            copy={copy}
            messages={messages}
            disabled={!isConnected || isGenerating || emergencyStopped}
            onSend={submitCommand}
            onOpenFullChat={() => onOpenAgentChat()}
          />
          <SystemInsightsCard copy={copy} metrics={telemetry.insights} />
        </div>
      </div>

      {!webglAvailable && (
        <p className="vx-empty" style={{ position: 'absolute', bottom: 8, left: 18, margin: 0 }} role="status">
          {copy.webglFallback}
        </p>
      )}

      {emergencyStopped && <VexaEmergencyOverlay copy={copy} onResume={handleResume} />}

      <VexaConfirmationDrawer
        copy={copy}
        open={confirmationsOpen}
        requests={telemetry.confirmations}
        onClose={() => setConfirmationsOpen(false)}
        onApprove={handleApprove}
        onReject={handleReject}
        onOpenProcesses={() => { setConfirmationsOpen(false); navigate('protocols'); }}
        emergencyStopped={emergencyStopped}
        onEmergencyStop={handleEmergencyStop}
        onResume={handleResume}
        simpleGraphics={simpleGraphics}
        onToggleGraphics={() => setSimpleGraphics(value => !value)}
      />
    </div>
  );
}
