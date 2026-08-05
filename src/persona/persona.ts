/**
 * Persona copy. Professional is the default voice; 修勾 (xiugou) is an opt-in
 * mode that swaps ONLY user-facing strings + accent — never layout, legal
 * content, citations, audit verdicts, or the disclaimer.
 */

import type { PersonaId } from "@/hooks/useTheme";

export type PersonaCopy = {
  brand: string;
  brandSub?: string;
  welcomeTitle: string;
  welcomeCaption: string;
  placeholder: string;
  avatar: string;
};

const XIUGOU_WELCOME = [
  "汪汪！赛博修勾今天帮麻麻看什么法条？",
  "修勾已就位，请下达法律检索指令！",
  "今天要翻哪本法典？修勾帮你刨。",
];

export function personaCopy(persona: PersonaId, welcomeIndex = 0): PersonaCopy {
  if (persona === "xiugou") {
    return {
      brand: "赛博修勾",
      welcomeTitle: XIUGOU_WELCOME[welcomeIndex % XIUGOU_WELCOME.length],
      welcomeCaption: "仅改变文案语气，不影响法律内容与引证 · 内容仅供参考",
      placeholder: "汪汪，麻麻今天有什么法律问题？",
      avatar: "🐶",
    };
  }
  return {
    brand: "法务助手",
    brandSub: "Legal Helper",
    welcomeTitle: "今天需要处理什么法律事务？",
    welcomeCaption: "内容仅供参考 · 正式使用前须经执业律师审核 / For reference only",
    placeholder: "描述您的法律问题，或拖入文件…",
    avatar: "⚖",
  };
}

export const DISCLAIMER = "不构成法律意见 / Not legal advice";

export const TASK_CHIPS: Array<{ icon: string; title: string; sub: string; skill?: string; prompt: string }> = [
  { icon: "📜", title: "检索法条", sub: "查找并精读现行有效条文", prompt: "帮我检索关于……的现行有效条文，并给出精读要点。" },
  { icon: "📑", title: "审查合同", sub: "上传合同，标注风险条款", skill: "review-contract", prompt: "请审查我上传的合同，标注风险条款并给出修改建议。" },
  { icon: "✍", title: "起草备忘录", sub: "生成带引证的法律备忘录", prompt: "请就……问题起草一份带引证的法律备忘录。" },
  { icon: "✈", title: "航空合规核查", sub: "CCAR / FAA / EASA 规章比对", skill: "compliance-check", prompt: "请就……进行航空合规核查，比对 CCAR/FAA/EASA 相关规章。" },
];
