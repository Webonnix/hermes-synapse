import { DiffViewer, looksLikeUnifiedDiff } from './DiffViewer';

type Props = { arguments: Record<string, unknown>; language: 'ru' | 'en' };
const FIELDS: [string, string, string][] = [
  ['recipient', 'Получатель', 'Recipient'], ['to', 'Получатель', 'Recipient'], ['email', 'Почта', 'Email'],
  ['phone', 'Телефон', 'Phone'], ['chat_id', 'Диалог', 'Conversation'], ['client_id', 'Клиент', 'Client'],
  ['attendees', 'Участники', 'Attendees'], ['url', 'Адрес', 'Destination'], ['domain', 'Домен', 'Domain'],
  ['path', 'Файл', 'File'], ['amount', 'Сумма', 'Amount'], ['currency', 'Валюта', 'Currency'],
  ['title', 'Название', 'Title'], ['date', 'Дата', 'Date'], ['time', 'Время', 'Time'],
  ['command', 'Команда', 'Command'], ['message', 'Сообщение', 'Message'], ['text', 'Текст', 'Text'],
];
function display(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value) && value.every(item => typeof item === 'string')) return value.join(', ');
  return JSON.stringify(value);
}
export function ApprovalPreview({ arguments: args, language }: Props) {
  const en = language === 'en';
  const fields = FIELDS.filter(([key]) => args[key] !== undefined && args[key] !== null);
  return <section className="contract-block approval-preview">
    <h4>{en ? 'Action preview' : 'Будет выполнено'}</h4>
    <p>{en ? 'Check the recipient, destination, amount and changes before approving.' : 'Проверьте получателя, адрес, сумму и изменения перед подтверждением.'}</p>
    {!!fields.length && <dl className="approval-preview-fields">{fields.map(([key, ru, english]) => <div key={key}>
      <dt>{en ? english : ru}</dt><dd>{display(args[key])}</dd>
    </div>)}</dl>}
    {Object.values(args).filter(value => typeof value === 'string' && looksLikeUnifiedDiff(value)).map((diff, index) => <DiffViewer key={index} diff={String(diff)} />)}
    <details><summary>{en ? 'All parameters' : 'Все параметры'}</summary><pre>{JSON.stringify(args, null, 2)}</pre></details>
  </section>;
}
