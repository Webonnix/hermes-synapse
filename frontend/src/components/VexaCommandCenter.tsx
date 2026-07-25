import {
  Activity,
  BrainCircuit,
  Check,
  Cpu,
  History,
  MessageSquare,
  Mic,
  MicOff,
  Plus,
  Radio,
  Send,
  ShieldCheck,
  Square,
  Users,
  Volume2,
  Wifi,
  WifiOff,
  X as XIcon,
} from 'lucide-react';
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import type { AgentModel, ChatMessage, ChatSession, ProviderBinding } from '../types';
import { VexaAudioAnalyser } from './vexaAudioAnalyser';

const VexaEnergyCore = lazy(() => import('./VexaEnergyCore'));

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
}

const COPY = {
  ru: {
    title: 'VEXA',
    subtitle: 'Автономный голосовой центр управления',
    ready: 'Готова к команде',
    listening: 'Слушаю',
    transcribing: 'Распознаю речь',
    thinking: 'Координирую агентов',
    speaking: 'Отвечаю',
    offline: 'Нет связи с ядром',
    error: 'Не расслышала, повторите',
    prompt: 'Скажите или напишите задачу для Vexa',
    send: 'Передать команду',
    stop: 'Остановить выполнение',
    mic: 'Начать голосовую команду',
    micStop: 'Завершить запись',
    conversation: 'Режим диалога',
    conversationHint: 'Vexa продолжит слушать после ответа',
    latestRequest: 'Последняя команда',
    latestAnswer: 'Ответ Vexa',
    waitingRequest: 'Ожидаю вашу команду.',
    waitingAnswer: 'Готова управлять агентами и выполнить задачу.',
    network: 'Контур',
    agents: 'Агенты',
    active: 'Активны',
    voice: 'Голос',
    local: 'Локальный',
    browser: 'Системный женский',
    policy: 'Контроль рисков включён',
    privacy: 'Микрофон активен только при синем индикаторе',
    secureContextRequired: 'Для микрофона нужен HTTPS или localhost',
    recognitionReady: 'Распознавание готово',
    recognitionLoading: 'Загружаю модель речи',
    agentMesh: 'Контур агентов',
    openChannel: 'Открыть канал с агентом',
    noTask: 'Ожидает задачу',
    noAgentsYet: 'Агенты ещё не загружены',
    history: 'История чатов',
    newChat: 'Новый чат',
    noHistory: 'Пока нет сохранённых чатов',
    seeAll: 'Показать все чаты',
    testModel: 'Тестовая модель',
    localModel: 'Локальная модель',
    externalModelPlaceholder: 'например: deepseek-chat',
    applySwitch: 'Применить',
    cancelSwitch: 'Отмена',
    testingWith: 'Тест на внешней модели:',
    usingLocalModel: 'Работает на локальной модели.',
    modelSwitchCaveat: 'Не влияет на делегирование под-агентам.',
  },
  en: {
    title: 'VEXA',
    subtitle: 'Autonomous voice command center',
    ready: 'Ready for a command',
    listening: 'Listening',
    transcribing: 'Transcribing speech',
    thinking: 'Coordinating agents',
    speaking: 'Responding',
    offline: 'Core connection unavailable',
    error: "Didn't catch that, try again",
    prompt: 'Speak or type a task for Vexa',
    send: 'Send command',
    stop: 'Stop execution',
    mic: 'Start a voice command',
    micStop: 'Finish recording',
    conversation: 'Conversation mode',
    conversationHint: 'Vexa will listen again after responding',
    latestRequest: 'Latest command',
    latestAnswer: 'Vexa response',
    waitingRequest: 'Awaiting your command.',
    waitingAnswer: 'Ready to coordinate agents and execute the task.',
    network: 'Core',
    agents: 'Agents',
    active: 'Active',
    voice: 'Voice',
    local: 'Local',
    browser: 'System female',
    policy: 'Risk controls enabled',
    privacy: 'The microphone is active only with the blue indicator',
    secureContextRequired: 'Microphone requires HTTPS or localhost',
    recognitionReady: 'Recognition ready',
    recognitionLoading: 'Loading speech model',
    agentMesh: 'Agent mesh',
    openChannel: 'Open a channel with an agent',
    noTask: 'Awaiting task',
    noAgentsYet: 'Agents have not loaded yet',
    history: 'Chat history',
    newChat: 'New chat',
    noHistory: 'No saved chats yet',
    seeAll: 'Show all chats',
    testModel: 'Test model',
    localModel: 'Local model',
    externalModelPlaceholder: 'e.g. deepseek-chat',
    applySwitch: 'Apply',
    cancelSwitch: 'Cancel',
    testingWith: 'Testing on external model:',
    usingLocalModel: 'Running on the local model.',
    modelSwitchCaveat: "Doesn't affect delegation to sub-agents.",
  },
} as const;

function plainText(value: string) {
  return value
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)]\([^)]*\)/g, '$1')
    .replace(/[#*_>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function phaseFor(
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

function phaseLabel(phase: VoicePhase, copy: typeof COPY.ru | typeof COPY.en) {
  return copy[phase];
}

function VexaCoreAnimation({
  phase,
  audioAnalyser,
  pulseKey,
}: {
  phase: VoicePhase;
  audioAnalyser: VexaAudioAnalyser;
  pulseKey: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const phaseRef = useRef(phase);
  const smoothedAmpRef = useRef(0);
  const pulsesRef = useRef<Array<{ start: number }>>([]);
  const lastPulseKeyRef = useRef(pulseKey);

  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);

  useEffect(() => {
    if (pulseKey !== lastPulseKeyRef.current) {
      lastPulseKeyRef.current = pulseKey;
      pulsesRef.current.push({ start: performance.now() });
    }
  }, [pulseKey]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    let frame = 0;
    let frameId = 0;

    const render = (time: number) => {
      const currentPhase = phaseRef.current;
      audioAnalyser.sync(currentPhase);

      const bounds = canvas.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const width = Math.max(1, Math.round(bounds.width * dpr));
      const height = Math.max(1, Math.round(bounds.height * dpr));
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;

      const liveAmp = audioAnalyser.read(currentPhase);
      smoothedAmpRef.current += (liveAmp - smoothedAmpRef.current) * .25;
      const voiceBoost = smoothedAmpRef.current;

      ctx.clearRect(0, 0, width, height);
      const cx = width / 2;
      const cy = height * .5;
      const radius = Math.min(width, height) * .31;
      const speed = currentPhase === 'listening' ? 2.4 : currentPhase === 'thinking' ? 1.7 : currentPhase === 'speaking' ? 2 : .7;
      const baseEnergy = currentPhase === 'offline' ? .16 : currentPhase === 'ready' ? .42 : .9;
      const energy = Math.min(1.4, baseEnergy + voiceBoost * .75);
      const tick = reduceMotion ? 0 : time / 1000;

      ctx.save();
      ctx.globalCompositeOperation = 'screen';
      for (let ring = 0; ring < 7; ring += 1) {
        const ringRadius = radius * (.48 + ring * .105) * (1 + voiceBoost * .04);
        const rotation = tick * speed * (ring % 2 ? -1 : 1) * (.12 + ring * .018);
        ctx.lineWidth = Math.max(1, dpr * (ring % 3 === 0 ? 1.25 : .65));
        ctx.strokeStyle = `rgba(${ring % 2 ? '58, 187, 255' : '27, 128, 255'}, ${.12 + energy * .2})`;
        for (let segment = 0; segment < 9; segment += 1) {
          const start = rotation + segment * Math.PI * 2 / 9;
          const length = .16 + ((ring * 7 + segment * 3) % 5) * .055;
          ctx.beginPath();
          ctx.arc(cx, cy, ringRadius, start, start + length);
          ctx.stroke();
        }
      }

      const particleCount = reduceMotion ? 28 : 74;
      for (let index = 0; index < particleCount; index += 1) {
        const seed = index * 12.9898;
        const orbit = radius * (.34 + ((Math.sin(seed) + 1) / 2) * .8);
        const angle = seed + tick * speed * (.08 + index % 5 * .018);
        const wobble = Math.sin(tick * 1.4 + seed) * radius * .025;
        const x = cx + Math.cos(angle) * (orbit + wobble);
        const y = cy + Math.sin(angle) * (orbit * .72 + wobble);
        const size = dpr * (.5 + (index % 4) * .32) * (1 + voiceBoost * .5);
        ctx.beginPath();
        ctx.arc(x, y, size, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(99, 213, 255, ${.12 + energy * ((index % 5) / 8 + .18)})`;
        ctx.fill();
      }

      if (currentPhase === 'listening' || currentPhase === 'speaking' || currentPhase === 'transcribing') {
        ctx.lineWidth = Math.max(1, dpr * 1.1);
        ctx.strokeStyle = `rgba(111, 224, 255, ${.25 + energy * .35})`;
        ctx.beginPath();
        for (let index = 0; index <= 96; index += 1) {
          const ratio = index / 96;
          const angle = ratio * Math.PI * 2;
          const signal = Math.sin(angle * 7 + tick * speed * 4) * .5 + Math.sin(angle * 13 - tick * 3) * .24;
          const waveRadius = radius * (.31 + signal * (.035 + voiceBoost * .09) * energy);
          const x = cx + Math.cos(angle) * waveRadius;
          const y = cy + Math.sin(angle) * waveRadius;
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.stroke();
      }

      const pulse = reduceMotion ? .65 : .6 + Math.sin(tick * (currentPhase === 'ready' ? 1.8 : 4.2)) * .14;
      const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius * .48);
      glow.addColorStop(0, `rgba(197, 244, 255, ${Math.min(1, pulse * energy + voiceBoost * .3)})`);
      glow.addColorStop(.13, `rgba(38, 174, 255, ${.35 * energy})`);
      glow.addColorStop(1, 'rgba(0, 76, 180, 0)');
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cy, radius * (.5 + voiceBoost * .06), 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();

      pulsesRef.current = pulsesRef.current.filter(item => time - item.start < 1300);
      pulsesRef.current.forEach(item => {
        const t = Math.min(1, (time - item.start) / 1300);
        ctx.beginPath();
        ctx.arc(cx, cy, radius * (.42 + t * 1.65), 0, Math.PI * 2);
        ctx.lineWidth = Math.max(1, dpr * 2.4 * (1 - t));
        ctx.strokeStyle = `rgba(167, 243, 255, ${(1 - t) * .5})`;
        ctx.stroke();
      });

      frame += 1;
      if (!reduceMotion || frame < 2) frameId = requestAnimationFrame(render);
    };

    frameId = requestAnimationFrame(render);
    return () => {
      cancelAnimationFrame(frameId);
      audioAnalyser.disconnectMic();
    };
  }, [audioAnalyser]);

  return <canvas ref={canvasRef} className="vexa-core-canvas" aria-hidden="true" />;
}

export function VexaCommandCenter({
  agents,
  messages,
  isConnected,
  isGenerating,
  isSpeaking,
  micState,
  micErrorMessage,
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
}: VexaCommandCenterProps) {
  const copy = COPY[language];
  const [input, setInput] = useState('');
  const [conversationMode, setConversationMode] = useState(false);
  const [ttsStatus, setTtsStatus] = useState<TtsStatus | null>(null);
  const [sttStatus, setSttStatus] = useState<SttStatus | null>(null);
  const lastAutoListenRef = useRef('');
  const [providers, setProviders] = useState<ProviderBinding[]>([]);
  const [pendingExternalId, setPendingExternalId] = useState<string | null>(null);
  const [pendingModelName, setPendingModelName] = useState('');
  const [jarvisSaving, setJarvisSaving] = useState(false);
  const jarvisAgent = useMemo(() => agents.find(agent => agent.id === 'jarvis'), [agents]);
  const [initialLocalModel] = useState(() => jarvisAgent?.model || 'qwen3:8b');
  const activeProviders = useMemo(() => providers.filter(p => p.status === 'active'), [providers]);
  const effectiveProviderValue = jarvisAgent && activeProviders.some(p => p.id === jarvisAgent.model_provider)
    ? jarvisAgent.model_provider!
    : 'ollama';
  const activeProviderName = activeProviders.find(p => p.id === effectiveProviderValue)?.name;
  const recentSessions = chatSessions.slice(0, 12);
  const phase = phaseFor(isConnected, micState, isGenerating, isSpeaking);
  const secureMicrophone = window.isSecureContext;
  const audioAnalyserRef = useRef<VexaAudioAnalyser | null>(null);
  if (!audioAnalyserRef.current) audioAnalyserRef.current = new VexaAudioAnalyser(micStreamRef, ttsAudioElRef);
  useEffect(() => () => audioAnalyserRef.current?.dispose(), []);
  const [energyCoreReady, setEnergyCoreReady] = useState(false);

  const conversation = useMemo(() => {
    const user = [...messages].reverse().find(message => message.role === 'user');
    const assistant = [...messages].reverse().find(message => message.role === 'assistant' && !message.streaming);
    return {
      request: user ? plainText(user.content) : copy.waitingRequest,
      answer: assistant ? plainText(assistant.content) : copy.waitingAnswer,
      answerKey: assistant ? `${assistant.id || ''}:${assistant.run_id || ''}:${assistant.content.length}` : '',
    };
  }, [copy.waitingAnswer, copy.waitingRequest, messages]);

  const activeAgents = useMemo(
    () => agents.filter(agent => ['working', 'running', 'active', 'processing'].includes(String(agent.status || '').toLowerCase())),
    [agents],
  );

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      fetch('/api/voice/tts/status').then(response => response.ok ? response.json() : Promise.reject()),
      fetch('/api/voice/status').then(response => response.ok ? response.json() : Promise.reject()),
    ]).then(([tts, stt]) => {
      if (cancelled) return;
      setTtsStatus(tts.status === 'fulfilled' ? tts.value : null);
      setSttStatus(stt.status === 'fulfilled' ? stt.value : null);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/providers', { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : []))
      .then(data => { if (!cancelled) setProviders(Array.isArray(data) ? data : []); })
      .catch(() => { if (!cancelled) setProviders([]); });
    return () => {
      cancelled = true;
    };
  }, []);

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
    if (value === 'ollama') {
      saveJarvisProvider('ollama', initialLocalModel);
    } else {
      setPendingExternalId(value);
      setPendingModelName('');
    }
  };

  useEffect(() => {
    if (!conversationMode || !conversation.answerKey || conversation.answerKey === lastAutoListenRef.current) return;
    if (!isConnected || isGenerating || isSpeaking || micState !== 'off') return;
    const answerKey = conversation.answerKey;
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
      onVoiceToggle();
    }, 1100);
    return () => window.clearTimeout(timer);
  }, [conversation.answerKey, conversationMode, isConnected, isGenerating, isSpeaking, micState, onVoiceToggle]);

  const submit = () => {
    const command = input.trim();
    if (!command || !isConnected || isGenerating) return;
    if (onCommand(command)) setInput('');
  };

  const toggleConversation = () => {
    if (!secureMicrophone) {
      onVoiceToggle();
      return;
    }
    const next = !conversationMode;
    setConversationMode(next);
    lastAutoListenRef.current = conversation.answerKey;
    if (next && micState === 'off' && !isGenerating && !isSpeaking) onVoiceToggle();
  };

  const voiceName = ttsStatus?.available
    ? `${copy.local} · ${ttsStatus.active_provider || 'TTS'}${ttsStatus.voice ? ` · ${ttsStatus.voice}` : ''}`
    : copy.browser;
  const recognitionName = !secureMicrophone
    ? copy.secureContextRequired
    : sttStatus?.loading
      ? copy.recognitionLoading
      : sttStatus?.enabled && sttStatus?.dependency_available
        ? `${copy.recognitionReady} · ${sttStatus.model}`
        : 'STT unavailable';

  return (
    <section className={`vexa-command-center is-${phase}${energyCoreReady ? ' is-energy-ready' : ''}`} aria-label={copy.subtitle}>
      <div className="vexa-backdrop" aria-hidden="true" />
      <VexaCoreAnimation phase={phase} audioAnalyser={audioAnalyserRef.current} pulseKey={conversation.answerKey} />
      <Suspense fallback={null}>
        <div className="vexa-energy-core-layer">
          <VexaEnergyCore phase={phase} audioAnalyser={audioAnalyserRef.current} pulseKey={conversation.answerKey} onReady={() => setEnergyCoreReady(true)} />
        </div>
      </Suspense>
      <div className="vexa-core-frame" aria-hidden="true">
        <i className="vexa-frame-corner is-tl" />
        <i className="vexa-frame-corner is-tr" />
        <i className="vexa-frame-corner is-bl" />
        <i className="vexa-frame-corner is-br" />
      </div>

      <header className="vexa-header">
        <div className="vexa-identity">
          <span className="vexa-kicker"><BrainCircuit size={15} /> Autonomous intelligence</span>
          <h1>{copy.title}</h1>
          <p>{copy.subtitle}</p>
        </div>
        <div className={`vexa-phase is-${phase}`} aria-live="polite" title={phase === 'error' ? micErrorMessage : undefined}>
          <i />
          <span>{phaseLabel(phase, copy)}</span>
        </div>
      </header>

      <div className="vexa-left-rail">
        <div className="vexa-telemetry" aria-label={language === 'ru' ? 'Состояние системы' : 'System state'}>
          <div>
            {isConnected ? <Wifi size={16} /> : <WifiOff size={16} />}
            <span>{copy.network}</span>
            <strong>{isConnected ? 'ONLINE' : 'OFFLINE'}</strong>
          </div>
          <div>
            <Users size={16} />
            <span>{copy.agents}</span>
            <strong>{agents.length}</strong>
          </div>
          <div>
            <Activity size={16} />
            <span>{copy.active}</span>
            <strong>{activeAgents.length}</strong>
          </div>
          <div>
            <Volume2 size={16} />
            <span>{copy.voice}</span>
            <strong title={voiceName}>{voiceName}</strong>
          </div>
        </div>

        {jarvisAgent && (
          <div className="vexa-model-switch">
            <div className="vexa-model-switch-head">
              <Cpu size={13} />
              <span>{copy.testModel}</span>
            </div>
            <select
              className="vexa-model-switch-select"
              value={pendingExternalId ?? effectiveProviderValue}
              disabled={jarvisSaving}
              onChange={event => handleProviderSelect(event.target.value)}
            >
              <option value="ollama">{copy.localModel}</option>
              {activeProviders.map(provider => (
                <option key={provider.id} value={provider.id}>{provider.name}</option>
              ))}
            </select>
            {pendingExternalId && (
              <div className="vexa-model-switch-confirm">
                <input
                  className="vexa-model-switch-model-input"
                  value={pendingModelName}
                  onChange={event => setPendingModelName(event.target.value)}
                  placeholder={copy.externalModelPlaceholder}
                  autoFocus
                />
                <button
                  type="button"
                  title={copy.applySwitch}
                  aria-label={copy.applySwitch}
                  disabled={!pendingModelName.trim() || jarvisSaving}
                  onClick={() => saveJarvisProvider(pendingExternalId, pendingModelName)}
                >
                  <Check size={13} />
                </button>
                <button
                  type="button"
                  title={copy.cancelSwitch}
                  aria-label={copy.cancelSwitch}
                  onClick={() => { setPendingExternalId(null); setPendingModelName(''); }}
                >
                  <XIcon size={13} />
                </button>
              </div>
            )}
            <p className="vexa-model-switch-hint">
              {effectiveProviderValue !== 'ollama' ? `${copy.testingWith} ${activeProviderName}` : copy.usingLocalModel}
              {' '}{copy.modelSwitchCaveat}
            </p>
          </div>
        )}

        <div className="vexa-history">
          <div className="vexa-history-head">
            <History size={14} />
            <span>{copy.history}</span>
            <button type="button" className="vexa-history-new" onClick={onCreateSession} title={copy.newChat} aria-label={copy.newChat}>
              <Plus size={13} />
            </button>
          </div>
          <div className="vexa-history-list">
            {recentSessions.length === 0 && <p>{copy.noHistory}</p>}
            {recentSessions.map(session => (
              <button
                type="button"
                key={session.id}
                className={currentChatId === session.id ? 'is-active' : ''}
                onClick={() => onOpenAgentChat(session.id)}
                title={session.title || getSessionLabel(session.id)}
              >
                <MessageSquare size={12} />
                <span>{session.title || getSessionLabel(session.id)}</span>
              </button>
            ))}
            {chatSessions.length > recentSessions.length && (
              <button type="button" onClick={() => onOpenAgentChat()}>
                <History size={12} />
                <span>{copy.seeAll}</span>
              </button>
            )}
          </div>
        </div>
      </div>

      <div className="vexa-transcript">
        <div>
          <span>{copy.latestRequest}</span>
          <p>{conversation.request}</p>
        </div>
        <div>
          <span>{copy.latestAnswer}</span>
          <p>{conversation.answer}</p>
        </div>
      </div>

      <aside className="vexa-agent-radar" aria-label={language === 'ru' ? 'Активные агенты' : 'Active agents'}>
        <div className="vexa-agent-radar-title">
          <Radio size={15} />
          <span>{copy.agentMesh}</span>
          <b>{activeAgents.length}/{agents.length}</b>
          <button type="button" className="vexa-agent-radar-open" onClick={() => onOpenAgentChat()} title={copy.openChannel} aria-label={copy.openChannel}>
            <MessageSquare size={14} />
          </button>
        </div>
        <div className="vexa-agent-list">
          {(activeAgents.length ? activeAgents : agents).slice(0, 7).map(agent => (
            <button type="button" key={agent.id} onClick={() => onOpenAgentChat(agent.id)} title={copy.openChannel}>
              <i className={activeAgents.includes(agent) ? 'is-active' : ''} />
              <span>{agent.name}</span>
              <small>{agent.current_task || agent.role || copy.noTask}</small>
            </button>
          ))}
          {!agents.length && <p>{copy.noAgentsYet}</p>}
        </div>
      </aside>

      <div className="vexa-voice-controls">
        <button
          type="button"
          className={`vexa-conversation-toggle${conversationMode ? ' is-active' : ''}`}
          onClick={toggleConversation}
          aria-disabled={!secureMicrophone}
          aria-pressed={conversationMode}
        >
          <Radio size={16} />
          <span>
            <strong>{copy.conversation}</strong>
            <small>{copy.conversationHint}</small>
          </span>
        </button>

        <button
          type="button"
          className={`vexa-mic-button${micState !== 'off' ? ' is-active' : ''}`}
          onClick={onVoiceToggle}
          disabled={micState === 'transcribing'}
          aria-label={micState === 'capturing' ? copy.micStop : copy.mic}
          title={micState === 'capturing' ? copy.micStop : copy.mic}
        >
          {micState === 'capturing' ? <MicOff size={28} /> : <Mic size={28} />}
          <span aria-hidden="true" />
        </button>

        {isGenerating ? (
          <button type="button" className="vexa-stop-button" onClick={onStop} title={copy.stop} aria-label={copy.stop}>
            <Square size={18} fill="currentColor" />
          </button>
        ) : (
          <div className="vexa-privacy">
            <ShieldCheck size={16} />
            <span>{copy.policy}</span>
          </div>
        )}
      </div>

      <form
        className="vexa-command-input"
        onSubmit={event => {
          event.preventDefault();
          submit();
        }}
      >
        <label htmlFor="vexa-command">{copy.prompt}</label>
        <input
          id="vexa-command"
          value={input}
          onChange={event => setInput(event.target.value)}
          placeholder={copy.prompt}
          autoComplete="off"
          disabled={!isConnected}
        />
        <button type="submit" disabled={!input.trim() || !isConnected || isGenerating} title={copy.send} aria-label={copy.send}>
          <Send size={18} />
        </button>
      </form>

      <footer className="vexa-footer">
        <span><i className={micState !== 'off' ? 'is-recording' : ''} /> {copy.privacy}</span>
        <span title={sttStatus?.last_error || recognitionName}>STT · {recognitionName}</span>
      </footer>
    </section>
  );
}
