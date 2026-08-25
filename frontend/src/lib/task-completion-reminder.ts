export type TaskCompletionReminderKind =
  | "outline"
  | "research"
  | "article"
  | "review";

export type TaskCompletionReminderStatus =
  | "unsupported"
  | "default"
  | "granted"
  | "denied";

type AudioContextConstructor = new () => AudioContext;

export type TaskCompletionReminderInput = {
  kind: TaskCompletionReminderKind;
  taskId: string;
  title: string;
  body: string;
};

let audioContext: AudioContext | null = null;
let permissionPromptAttempted = false;

function notificationStatus(): TaskCompletionReminderStatus {
  if (typeof window === "undefined" || !("Notification" in window)) {
    return "unsupported";
  }
  if (Notification.permission === "granted") return "granted";
  if (Notification.permission === "denied") return "denied";
  return "default";
}

function audioContextConstructor(): AudioContextConstructor | null {
  if (typeof window === "undefined") return null;
  const browserWindow = window as Window & {
    webkitAudioContext?: AudioContextConstructor;
  };
  return window.AudioContext || browserWindow.webkitAudioContext || null;
}

function getAudioContext() {
  if (typeof window === "undefined") return null;
  if (audioContext) return audioContext;
  const Constructor = audioContextConstructor();
  if (!Constructor) return null;
  try {
    audioContext = new Constructor();
    return audioContext;
  } catch {
    return null;
  }
}

export function getTaskCompletionReminderStatus(): TaskCompletionReminderStatus {
  return notificationStatus();
}

/**
 * Must be called from a user gesture before a long-running job starts. It
 * unlocks audio for the eventual completion callback and requests the browser
 * notification permission once without blocking the actual job on failure.
 */
export async function prepareTaskCompletionReminders() {
  let permissionRequest: Promise<NotificationPermission> | null = null;
  if (
    typeof window !== "undefined" &&
    "Notification" in window &&
    Notification.permission === "default" &&
    !permissionPromptAttempted
  ) {
    permissionPromptAttempted = true;
    // Start this call before the first await so browser user-gesture rules
    // still apply when the function is called by a generation button.
    permissionRequest = Notification.requestPermission().catch(
      () => Notification.permission,
    );
  }

  const context = getAudioContext();
  if (context?.state === "suspended") {
    await context.resume().catch(() => undefined);
  }

  await permissionRequest;

  return notificationStatus();
}

async function playCompletionTone() {
  const context = getAudioContext();
  if (!context) return;
  await context.resume().catch(() => undefined);
  if (context.state !== "running") return;

  const now = context.currentTime;
  const gain = context.createGain();
  gain.gain.setValueAtTime(0.0001, now);
  gain.gain.exponentialRampToValueAtTime(0.12, now + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.42);
  gain.connect(context.destination);

  for (const [offset, frequency] of [
    [0, 660],
    [0.16, 880],
  ] as const) {
    const oscillator = context.createOscillator();
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(frequency, now + offset);
    oscillator.connect(gain);
    oscillator.start(now + offset);
    oscillator.stop(now + offset + 0.18);
  }
}

export async function notifyTaskCompletion({
  kind,
  taskId,
  title,
  body,
}: TaskCompletionReminderInput) {
  await playCompletionTone();

  if (notificationStatus() !== "granted") return;

  try {
    const notification = new Notification(title, {
      body,
      tag: `article-agent:${kind}:${taskId}`,
    });
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
  } catch {
    // Notification construction can still fail in a restricted browser
    // context; the completion tone and in-page success message remain useful.
  }
}
