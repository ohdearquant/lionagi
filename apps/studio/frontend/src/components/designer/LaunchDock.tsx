/**
 * LaunchDock — the bottom action bar of the launch console.
 * Name + prompt + Save + Launch in one strip: the canvas above is the
 * configuration, this is the trigger. Launch saves first when needed so a
 * valid draft goes from blank canvas to running engine in one gesture.
 */
import { useCallback, useState } from "react";
import { useTranslations } from "use-intl";
import { createEngineDef, updateEngineDef, launchEngine } from "@/lib/api";
import { buildDefBody, validateDraft } from "@/lib/designer/draft";
import type { EngineDefDraft } from "@/lib/designer/draft";
import { ENGINE_TOPOLOGIES } from "@/lib/designer/topology";
import { useToast } from "@/components/ui/Toast";
import { Input } from "@/components/ui/Field";
import Button from "@/components/ui/Button";
import { IconSave, IconLaunch } from "@/components/ui/icons";

export interface LaunchDockProps {
  draft: EngineDefDraft;
  patchDraft: (patch: Partial<EngineDefDraft>) => void;
  existingId: string | null;
  onSaved: (id: string, name: string) => void;
  advancedOpen: boolean;
  onToggleAdvanced: () => void;
}

export default function LaunchDock({
  draft,
  patchDraft,
  existingId,
  onSaved,
  advancedOpen,
  onToggleAdvanced,
}: LaunchDockProps) {
  const t = useTranslations("designer.dock");
  const { toast } = useToast();
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState<"save" | "launch" | null>(null);

  const topo = ENGINE_TOPOLOGIES[draft.kind];

  const errorMessage = useCallback(
    (errors: Record<string, string>): string => {
      if (errors.name) return t("nameRequired");
      if (errors.test_cmd) return t("testCmdRequired");
      return t("invalidBounds");
    },
    [t],
  );

  /** Create or update the def; returns its id + name, or null on failure. */
  const persist = useCallback(async (): Promise<{ id: string; name: string } | null> => {
    const errors = validateDraft(draft, topo);
    if (Object.keys(errors).length > 0) {
      toast(errorMessage(errors), "error");
      return null;
    }
    const body = buildDefBody(draft);
    if (existingId) {
      await updateEngineDef(existingId, body);
      onSaved(existingId, draft.name);
      return { id: existingId, name: draft.name };
    }
    const res = await createEngineDef(body);
    onSaved(res.id, res.name);
    return { id: res.id, name: res.name };
  }, [draft, topo, existingId, onSaved, toast, errorMessage]);

  const handleSave = useCallback(async () => {
    setBusy("save");
    try {
      const saved = await persist();
      if (saved) toast(t("saved"), "success");
    } catch (err) {
      toast(String(err), "error");
    } finally {
      setBusy(null);
    }
  }, [persist, toast, t]);

  const handleLaunch = useCallback(async () => {
    if (!prompt.trim()) {
      toast(t("promptRequired"), "error");
      return;
    }
    setBusy("launch");
    try {
      const saved = await persist();
      if (!saved) return;
      const res = await launchEngine({
        action_kind: "engine",
        action_engine_def: saved.name,
        action_prompt: prompt.trim(),
      });
      toast(`${t("launched")} ${res.invocation_id}`, "success");
      setPrompt("");
    } catch (err) {
      toast(String(err), "error");
    } finally {
      setBusy(null);
    }
  }, [prompt, persist, toast, t]);

  return (
    <div className="flex shrink-0 items-center gap-2 border-t border-edge bg-surface-raised px-3 py-2.5">
      <Input
        type="text"
        mono
        value={draft.name}
        onChange={(e) => patchDraft({ name: e.target.value })}
        placeholder={t("namePlaceholder")}
        aria-label={t("namePlaceholder")}
        className="w-[200px] shrink-0"
      />
      <Input
        type="text"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && prompt.trim() && busy === null) void handleLaunch();
        }}
        placeholder={t("promptPlaceholder")}
        aria-label={t("promptPlaceholder")}
        className="min-w-0 flex-1"
      />
      <Button
        variant="toggle"
        active={advancedOpen}
        onClick={onToggleAdvanced}
        title={t("advanced")}
      >
        {t("advanced")}
      </Button>
      <Button
        variant="secondary"
        onClick={() => void handleSave()}
        disabled={busy !== null}
        leading={<IconSave size={12} aria-hidden="true" />}
      >
        {busy === "save" ? t("saving") : existingId ? t("saveChanges") : t("save")}
      </Button>
      <Button
        variant="primary"
        onClick={() => void handleLaunch()}
        disabled={busy !== null || !prompt.trim()}
        leading={<IconLaunch size={12} aria-hidden="true" />}
      >
        {busy === "launch" ? t("launching") : t("launch")}
      </Button>
    </div>
  );
}
