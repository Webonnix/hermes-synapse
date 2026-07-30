/**
 * All user-facing strings for the VEXA dashboard, RU/EN.
 *
 * Several keys are load-bearing for accessibility tests and for muscle memory of the
 * existing UI (`ready`, `prompt`, `send`, `mic`, `conversation`, `openChannel`) — keep
 * their Russian wording stable unless the tests are updated alongside.
 */

export const VEXA_COPY = {
  ru: {
    // brand + header
    brand: 'VEXA',
    brandSub: 'AI Voice System',
    systemOnline: 'Система онлайн',
    systemDegraded: 'Система в деградации',
    systemMaintenance: 'Обслуживание',
    systemOffline: 'Нет связи с ядром',
    systemEmergency: 'Аварийная остановка',
    systemOnlineHint: 'Все системы работают в штатном режиме',
    systemDegradedHint: 'Часть подсистем недоступна',
    systemOfflineHint: 'Соединение с ядром потеряно',
    systemStaleHint: 'Данные устарели',
    simpleMode: 'Простой режим',
    lightGraphics: 'Экономная графика',
    appMenu: 'Меню Hermes',
    fullscreen: 'Полноэкранный режим',
    exitFullscreen: 'Выйти из полноэкранного режима',
    quickTelemetry: 'Телеметрия и аналитика',
    quickAgents: 'Агенты',
    quickSecurity: 'Процессы и контроль рисков',
    quickNotifications: 'Очередь подтверждений',
    quickSettings: 'Настройки',

    // runtime state pill
    ready: 'Готова к команде',
    listening: 'Слушаю',
    transcribing: 'Распознаю',
    thinking: 'Анализирую',
    executing: 'Выполняю',
    awaitingConfirmation: 'Ожидаю подтверждения',
    speaking: 'Отвечаю',
    offline: 'Нет соединения',
    error: 'Не расслышала, повторите',
    emergencyStopped: 'Аварийно остановлена',

    // system status card
    systemStatus: 'Системный статус',
    circuit: 'Контур',
    agents: 'Агенты',
    active: 'Активные',
    voice: 'Голос',
    cpuLoad: 'Нагрузка CPU',
    memory: 'Память',
    network: 'Сеть',
    uptime: 'Аптайм',
    local: 'Локальный',
    browser: 'Системный',
    noData: 'Нет данных',

    // model selector
    testModel: 'Тестовая модель',
    localModel: 'Локальная модель',
    usingLocalModel: 'Работает на локальной модели.',
    modelSwitchCaveat: 'Не влияет на делегирование под-агентам.',
    testingWith: 'Тест на внешней модели:',
    externalModelPlaceholder: 'например: deepseek-chat',
    applySwitch: 'Применить',
    cancelSwitch: 'Отмена',
    modelSwitchBusy: 'Идёт генерация ответа. Переключение модели остановит её. Продолжить?',

    // chat history
    history: 'История чатов',
    newChat: 'Новый чат',
    noHistory: 'Пока нет сохранённых чатов',
    seeAll: 'Смотреть все',

    // neural density
    density: 'Нейронная плотность',
    densityHigh: 'Высокая активность',
    densityMedium: 'Умеренная активность',
    densityLow: 'Низкая активность',

    // resources
    resources: 'Ресурсы системы',

    // neural activity
    activity: 'Нейронная активность',
    liveFeed: 'Live Feed',

    // data stream
    dataStream: 'Поток данных',

    // protocols
    protocols: 'Активные протоколы',
    statusRunning: 'RUNNING',
    statusStandby: 'STANDBY',
    statusPaused: 'PAUSED',
    statusError: 'ERROR',
    statusOffline: 'OFFLINE',
    protocolLatency: 'Задержка',
    protocolVersion: 'Версия',
    protocolState: 'Статус',
    protocolLastError: 'Ошибка',

    // core HUD
    hudAnalyzing: 'Analyzing',
    hudDataPatterns: 'Data patterns',
    hudNeuralCore: 'Neural core',
    hudLinkStrength: 'Link strength',
    hudThoughtFlow: 'Thought flow',
    coreVisualization: 'Интерактивная визуализация нейронного ядра VEXA. Текущее состояние',

    // voice
    mic: 'Начать голосовую команду',
    micStop: 'Завершить запись',
    micUnavailable: 'Микрофон недоступен',
    secureContextRequired: 'Для микрофона нужен HTTPS или localhost',
    conversation: 'Режим диалога',
    conversationHint: 'Vexa продолжит слушать после ответа',
    riskControl: 'Контроль рисков',
    riskEnabled: 'Включён',
    riskPending: 'подтверждений в очереди',
    riskStopped: 'Остановлено',

    // composer
    prompt: 'Скажите или напишите задачу для Vexa',
    send: 'Передать команду',
    stop: 'Остановить выполнение',
    sendFailed: 'Команда не отправлена. Попробуйте ещё раз.',

    // bottom navigation
    navTerminal: 'Терминал',
    navAnalytics: 'Аналитика',
    navAgents: 'Агенты',
    navProtocols: 'Протоколы',
    navSettings: 'Настройки',

    // agents
    agentCircuit: 'Контур агентов',
    openChannel: 'Открыть канал с агентом',
    seeAllAgents: 'Смотреть всех агентов',
    noAgentsYet: 'Агенты ещё не загружены',
    specialist: 'Specialist',
    noTask: 'Ожидает задачу',

    // conversation
    conversationTitle: 'Разговор с Vexa',
    askFollowUp: 'Задайте уточняющий вопрос...',
    conversationEmpty: 'Диалог пуст. Задайте первый вопрос.',
    openFullChat: 'Открыть полный чат',

    // insights
    insights: 'Системные инсайты',
    efficiency: 'Эффективность',
    responseTime: 'Время ответа',
    accuracy: 'Точность',
    stability: 'Стабильность',

    // confirmations + emergency
    confirmations: 'Очередь подтверждений',
    noConfirmations: 'Нет действий, ожидающих подтверждения.',
    approve: 'Подтвердить',
    reject: 'Отклонить',
    riskLevel: 'Риск',
    requested: 'Запрошено',
    emergencyStop: 'Аварийная остановка',
    emergencyHold: 'Удерживайте для аварийной остановки',
    emergencyActive: 'Система аварийно остановлена',
    resume: 'Возобновить',
    resumeConfirm: 'Возобновить работу агентов после аварийной остановки?',
    close: 'Закрыть',

    // fallback
    webglFallback: 'Аппаратное ускорение недоступно — включён упрощённый режим визуализации.',
    version: 'VEXA AI VOICE SYSTEM',
  },
  en: {
    brand: 'VEXA',
    brandSub: 'AI Voice System',
    systemOnline: 'System online',
    systemDegraded: 'System degraded',
    systemMaintenance: 'Maintenance',
    systemOffline: 'Core connection lost',
    systemEmergency: 'Emergency stop',
    systemOnlineHint: 'All systems nominal',
    systemDegradedHint: 'Some subsystems are unavailable',
    systemOfflineHint: 'Connection to the core was lost',
    systemStaleHint: 'Data is stale',
    simpleMode: 'Simple mode',
    lightGraphics: 'Light graphics',
    appMenu: 'Hermes menu',
    fullscreen: 'Fullscreen',
    exitFullscreen: 'Exit fullscreen',
    quickTelemetry: 'Telemetry & analytics',
    quickAgents: 'Agents',
    quickSecurity: 'Processes & risk control',
    quickNotifications: 'Confirmation queue',
    quickSettings: 'Settings',

    ready: 'Ready for a command',
    listening: 'Listening',
    transcribing: 'Transcribing',
    thinking: 'Analyzing',
    executing: 'Executing',
    awaitingConfirmation: 'Awaiting approval',
    speaking: 'Responding',
    offline: 'No connection',
    error: "Didn't catch that, try again",
    emergencyStopped: 'Emergency stopped',

    systemStatus: 'System status',
    circuit: 'Core',
    agents: 'Agents',
    active: 'Active',
    voice: 'Voice',
    cpuLoad: 'CPU load',
    memory: 'Memory',
    network: 'Network',
    uptime: 'Uptime',
    local: 'Local',
    browser: 'System',
    noData: 'No data',

    testModel: 'Test model',
    localModel: 'Local model',
    usingLocalModel: 'Running on the local model.',
    modelSwitchCaveat: "Doesn't affect delegation to sub-agents.",
    testingWith: 'Testing on external model:',
    externalModelPlaceholder: 'e.g. deepseek-chat',
    applySwitch: 'Apply',
    cancelSwitch: 'Cancel',
    modelSwitchBusy: 'A response is being generated. Switching models will stop it. Continue?',

    history: 'Chat history',
    newChat: 'New chat',
    noHistory: 'No saved chats yet',
    seeAll: 'See all',

    density: 'Neural density',
    densityHigh: 'High activity',
    densityMedium: 'Moderate activity',
    densityLow: 'Low activity',

    resources: 'System resources',

    activity: 'Neural activity',
    liveFeed: 'Live Feed',

    dataStream: 'Data stream',

    protocols: 'Active protocols',
    statusRunning: 'RUNNING',
    statusStandby: 'STANDBY',
    statusPaused: 'PAUSED',
    statusError: 'ERROR',
    statusOffline: 'OFFLINE',
    protocolLatency: 'Latency',
    protocolVersion: 'Version',
    protocolState: 'Status',
    protocolLastError: 'Error',

    hudAnalyzing: 'Analyzing',
    hudDataPatterns: 'Data patterns',
    hudNeuralCore: 'Neural core',
    hudLinkStrength: 'Link strength',
    hudThoughtFlow: 'Thought flow',
    coreVisualization: 'Interactive VEXA neural core visualization. Current state',

    mic: 'Start a voice command',
    micStop: 'Finish recording',
    micUnavailable: 'Microphone unavailable',
    secureContextRequired: 'Microphone requires HTTPS or localhost',
    conversation: 'Conversation mode',
    conversationHint: 'Vexa will listen again after responding',
    riskControl: 'Risk control',
    riskEnabled: 'Enabled',
    riskPending: 'approvals queued',
    riskStopped: 'Stopped',

    prompt: 'Speak or type a task for Vexa',
    send: 'Send command',
    stop: 'Stop execution',
    sendFailed: 'Command was not sent. Try again.',

    navTerminal: 'Terminal',
    navAnalytics: 'Analytics',
    navAgents: 'Agents',
    navProtocols: 'Protocols',
    navSettings: 'Settings',

    agentCircuit: 'Agent circuit',
    openChannel: 'Open a channel with an agent',
    seeAllAgents: 'See all agents',
    noAgentsYet: 'Agents have not loaded yet',
    specialist: 'Specialist',
    noTask: 'Awaiting task',

    conversationTitle: 'Conversation with Vexa',
    askFollowUp: 'Ask a follow-up question...',
    conversationEmpty: 'No messages yet. Ask the first question.',
    openFullChat: 'Open the full chat',

    insights: 'System insights',
    efficiency: 'Efficiency',
    responseTime: 'Response time',
    accuracy: 'Accuracy',
    stability: 'Stability',

    confirmations: 'Confirmation queue',
    noConfirmations: 'Nothing is awaiting approval.',
    approve: 'Approve',
    reject: 'Reject',
    riskLevel: 'Risk',
    requested: 'Requested',
    emergencyStop: 'Emergency stop',
    emergencyHold: 'Hold to trigger the emergency stop',
    emergencyActive: 'System is emergency stopped',
    resume: 'Resume',
    resumeConfirm: 'Resume agent execution after the emergency stop?',
    close: 'Close',

    webglFallback: 'Hardware acceleration is unavailable — simplified visualization is on.',
    version: 'VEXA AI VOICE SYSTEM',
  },
} as const;

/**
 * Widened to `string` per key: `as const` above gives each entry a literal type, which
 * would otherwise make the RU and EN dictionaries mutually unassignable at the call site.
 */
export type VexaCopy = { [K in keyof typeof VEXA_COPY.ru]: string };
export type VexaLanguage = keyof typeof VEXA_COPY;
