/** The voice words (lib/voiceWords) for a component that names them — the
 * mic's title and options, Settings → Voice, the voice tour: re-renders the
 * caller whenever they change (GET /api/voice answered, a save). */

import { useSyncExternalStore } from "react";
import { onVoiceWords, voiceWords, type VoiceWords } from "./voiceWords";

export function useVoiceWords(): VoiceWords {
  return useSyncExternalStore(onVoiceWords, voiceWords, voiceWords);
}
