/** Feedback (backend core/feedback.py, docs/web-ui.md "Feedback"): report a
 * problem or an idea about MindFlock itself. One box, two ways in — the
 * person opens it (the sidebar's ? Help → Send feedback…, the palette) and
 * types, or an agent they are talking to drafts a report (the MCP's
 * `report_issue`) and the box opens on that draft. Nothing is sent until
 * Send; sent, it is a public GitHub issue. */

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { create } from "zustand";
import { api } from "../api/client";
import { queryClient } from "../state/queries";
import { toast } from "./toast";

export type FeedbackKind = "bug" | "idea" | "feedback";

export interface FeedbackDraft {
  id: string;
  title: string;
  body: string;
  kind: FeedbackKind;
  session: string;
  created: number;
  removed: number;
}

export interface FeedbackInfo {
  repo: string;
  /** The one line MindFlock adds (its version and the operating system). */
  about: string;
  drafts: FeedbackDraft[];
  signed_in: boolean;
  login: string;
}

export interface FeedbackSent {
  ok: boolean;
  via: "github" | "browser";
  url: string;
  number?: number;
  why?: string;
  removed?: number;
}

export const KIND_LABEL: Record<FeedbackKind, string> = {
  bug: "Something is broken",
  idea: "An idea",
  feedback: "Other feedback",
};

interface FeedbackUi {
  open: boolean;
  /** Open on this draft (an agent's); null = whatever is waiting, or blank. */
  draftId: string | null;
  /** Bumped per open, so the dialog starts fresh each time. */
  n: number;
  show(draftId?: string | null): void;
  close(): void;
}

export const useFeedbackUi = create<FeedbackUi>((set, get) => ({
  open: false,
  draftId: null,
  n: 0,
  show: (draftId = null) => set({ open: true, draftId, n: get().n + 1 }),
  close: () => set({ open: false, draftId: null }),
}));

export function openFeedback(draftId?: string | null): void {
  useFeedbackUi.getState().show(draftId ?? null);
}

export function loadFeedback(): Promise<FeedbackInfo> {
  return api<FeedbackInfo>("/api/feedback");
}

const FEEDBACK_KEY = ["feedback"];

/** How many reports an agent drafted that wait for the person to read — the
 * calm mark on the sidebar's ? Help and its Send feedback… row, so a draft
 * whose toast went by is not lost. Read once, then again when an agent
 * drafts one (`feedback.draft`) and whenever the box closes (sent or
 * discarded); never polled. A guest (or a failed read) has none. */
export function useFeedbackDrafts(enabled = true): number {
  const open = useFeedbackUi((s) => s.open);
  useEffect(() => {
    if (!enabled) return;
    const ev = typeof window !== "undefined" ? window.mindflock?.events : undefined;
    if (!ev) return;
    return ev.subscribe("feedback.draft", () => void queryClient.invalidateQueries({ queryKey: FEEDBACK_KEY }));
  }, [enabled]);
  useEffect(() => {
    if (enabled && !open) void queryClient.invalidateQueries({ queryKey: FEEDBACK_KEY });
  }, [enabled, open]);
  const q = useQuery({
    queryKey: FEEDBACK_KEY,
    queryFn: loadFeedback,
    enabled,
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  });
  return enabled && !q.isError ? q.data?.drafts?.length || 0 : 0;
}

export function discardDraft(id: string): Promise<unknown> {
  return api("/api/feedback/drafts/" + encodeURIComponent(id), { method: "DELETE" });
}

export function sendFeedback(body: {
  title: string;
  body: string;
  kind: FeedbackKind;
  include_about: boolean;
  draft_id?: string;
}): Promise<FeedbackSent> {
  return api<FeedbackSent>("/api/feedback/send", { json: body });
}

/** What the box says under the text, before Send: where it goes and as whom. */
export function destinationLine(info: Pick<FeedbackInfo, "repo" | "signed_in" | "login"> | null): string {
  if (!info) return "";
  if (info.signed_in && info.login) return "Posted publicly on GitHub (" + info.repo + ") as " + info.login + ".";
  if (info.signed_in) return "Posted publicly on GitHub (" + info.repo + ").";
  return "Opens a pre-filled GitHub page (" + info.repo + ") for you to submit. It will be public.";
}

/** The toast after Send. */
export function sentLine(r: FeedbackSent): string {
  if (r.via === "github") return "Thanks — filed as issue #" + (r.number ?? "") + ".";
  return "Opened GitHub with your report filled in — press Submit there.";
}

/** Which draft the box opens on: the one asked for, else the newest. */
export function pickDraft(drafts: FeedbackDraft[], want: string | null): FeedbackDraft | null {
  if (want) {
    const hit = drafts.find((d) => d.id === want);
    if (hit) return hit;
  }
  return drafts[0] || null;
}

let bridged = false;
/** An agent drafted a report: offer it (a toast with Review), never a dialog
 * that takes the keyboard from someone typing. */
export function bridgeFeedbackEvents(): void {
  if (bridged) return;
  const ev = typeof window !== "undefined" ? window.mindflock?.events : undefined;
  if (!ev) return;
  bridged = true;
  ev.subscribe("feedback.draft", (env) => {
    const d = (env.data || {}) as { id?: string; title?: string };
    if (useFeedbackUi.getState().open) return;
    toast("A report about MindFlock is ready for you to review" + (d.title ? ": " + d.title : "."), {
      action: { label: "Review", onClick: () => openFeedback(d.id || null) },
      duration: 15000,
    });
  });
}
