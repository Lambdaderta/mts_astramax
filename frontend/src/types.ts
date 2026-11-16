export type SessionStatus = 'none' | 'wait' | 'specialist' | 'nokb' | 'request';
import type { components } from './api/schema';
export type ApiPreview = components['schemas']['Preview'];
export type ApiDraft = components['schemas']['Draft'];
export type ApiLead = components['schemas']['Lead'];
export type ApiTrace = components['schemas']['Trace'];
export type Stage = 'start' | 'clarify' | 'meter' | 'products' | 'offer' | 'form' | 'preview' | 'consulting' | 'sent' | 'nokb' | 'scope' | 'solar' | 'return';
export interface Product { id: string; name: string; subtitle: string; description: string; recommended: boolean; }
export interface DebugInfo {
  responseMs: number;
  confidence: 'высокая' | 'низкая' | 'не указана';
  clarification: boolean;
  tag?: 'нет в базе' | 'вне компетенции';
  documents: { path: string; score: number | null; chunkId?: string; quote?: string }[];
  reason?: string;
  query?: string;
  corpusVersion?: string;
  timings?: Record<string, number>;
}
export interface Message {
  id: string;
  role: 'user' | 'bot';
  text: string;
  delivery?: 'sent' | 'failed';
  debug?: DebugInfo;
  note?: string;
  seq?: number;
  clientMessageId?: string;
  error?: string;
  retryable?: boolean;
  retryAt?: number;
}
export interface Lead {
  id: string; sessionId: string; fullName: string; phone: string; company: string;
  consent: boolean; createdAt: string; summary: string;
}
export interface Session {
  id: string; title: string; createdAt: string; status: SessionStatus; archived: boolean;
  messages: Message[]; stage: Stage; context: { topic: 'bill' | 'solar' | 'snt'; meter?: string };
  lead?: Lead;
  source?: 'api';
  greeting?: string;
  draft?: ApiDraft;
  preview?: ApiPreview;
  apiLead?: ApiLead;
  confirmationMessage?: string;
  accessError?: string;
}
export interface ArchiveFile { id: string; name: string; date: string; user: string; }
export interface LeadInput { fullName: string; phone: string; company: string; consent: boolean; }
export type TurnProgress = components['schemas']['TurnProgress'];
export interface SendOptions {
  retryId?: string;
  simulateError?: boolean;
  signal?: AbortSignal;
  onProgress?: (progress?: TurnProgress) => void;
}
export interface ChatService {
  readonly mode?: 'mock' | 'api';
  readonly reviewAvailable?: boolean;
  initialize?(): Promise<Session>;
  getSession?(id: string): Promise<Session>;
  getDraft?(id: string): Promise<ApiDraft>;
  previewLead?(id: string, input: LeadInput): Promise<Session>;
  confirmLead?(id: string): Promise<Session>;
  editLead?(id: string): Promise<Session>;
  getTrace?(messageId: string): Promise<DebugInfo>;
  listReviewSessions?(): Promise<Session[]>;
  getReviewSession?(id: string): Promise<Session>;
  listReviewLeads?(): Promise<components['schemas']['ReviewLeadPage']['items']>;
  getReviewLead?(id: string): Promise<ApiLead>;
  listSessions(): Promise<Session[]>;
  createSession(fromSessionId?: string): Promise<Session>;
  sendMessage(sessionId: string, text: string, options?: SendOptions): Promise<Session>;
  submitLead(sessionId: string, input: LeadInput): Promise<Session>;
  listArchive(): Promise<ArchiveFile[]>;
  downloadArchive(id: string): Promise<Blob>;
}
