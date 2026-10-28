// M3 Expressive motion physics: пружины → CSS linear() (там, где поддерживается), иначе cubic-bezier из tokens.css.
// View Transitions для container transform, WAAPI для shared axis. prefers-reduced-motion → без анимации.

const mq = matchMedia("(prefers-reduced-motion: reduce)");
export const reducedMotion = () => mq.matches;

// Переходная характеристика пружины (масса 1): x(t) от 0 к 1
function springCurve(damping, stiffness) {
  const w = Math.sqrt(stiffness);
  const z = damping;
  const x = (t) => {
    if (z < 1) {
      const wd = w * Math.sqrt(1 - z * z);
      return 1 - Math.exp(-z * w * t) * (Math.cos(wd * t) + ((z * w) / wd) * Math.sin(wd * t));
    }
    return 1 - Math.exp(-w * t) * (1 + w * t);
  };
  // время успокоения: |1-x| < 0.002 до конца
  let T = 0.05;
  for (let t = 0.05; t < 3; t += 0.01) { if (Math.abs(1 - x(t)) > 0.002) T = t + 0.01; }
  const pts = [];
  const n = 40;
  for (let i = 0; i <= n; i++) pts.push(i === n ? 1 : +x((i / n) * T).toFixed(4));
  return { easing: `linear(${pts.join(", ")})`, ms: Math.round(T * 1000) };
}

// Токены M3E (expressive scheme): spatial — с перелётом, effects — без
export const SPRINGS = {
  "spatial-fast": [0.6, 800], "spatial-default": [0.8, 380], "spatial-slow": [0.8, 200],
  "effects-fast": [1, 3800], "effects-default": [1, 1600], "effects-slow": [1, 800],
};

export const motion = {};

export function installMotion() {
  const st = document.documentElement.style;
  const linearOk = CSS.supports("transition-timing-function", "linear(0, 0.5, 1)");
  for (const [name, [d, k]] of Object.entries(SPRINGS)) {
    const s = springCurve(d, k);
    motion[name] = { easing: linearOk ? s.easing : getComputedStyle(document.documentElement).getPropertyValue(`--md-motion-${name}`).trim() || "ease", ms: s.ms };
    if (linearOk) {
      st.setProperty(`--md-motion-${name}`, s.easing);
      st.setProperty(`--md-motion-${name}-dur`, s.ms + "ms");
    }
  }
}

export function animate(node, keyframes, token = "spatial-default", extra = {}) {
  if (reducedMotion() || !node.animate) return null;
  const m = motion[token] || { easing: "ease", ms: 300 };
  try { return node.animate(keyframes, { duration: m.ms, easing: m.easing, fill: "both", ...extra }); } catch { return null; }
}

// Shared axis X: входящий экран сдвигается с направления навигации
export function sharedAxis(node, dir = 1) {
  const a = animate(node, [
    { opacity: 0, transform: `translateX(${dir * 32}px)` },
    { opacity: 1, transform: "none" },
  ], "spatial-default");
  if (a) a.finished.then(() => a.cancel(), () => {});
  return a;
}

// Shared axis X целиком (M3): уходящее гаснет со сдвигом против направления, swap() меняет содержимое
// невидимым, входящее въезжает с направления. Повторный вызов посреди ухода подхватывает текущую прозрачность.
const swaps = new WeakMap();
export function axisSwap(node, dir, swap) {
  const run = (swaps.get(node) || 0) + 1;
  swaps.set(node, run);
  if (reducedMotion() || !node.animate) { swap(); return; }
  const cs = getComputedStyle(node);
  const from = { opacity: cs.opacity, transform: cs.transform === "none" ? "none" : cs.transform };
  for (const a of node.getAnimations()) a.cancel();
  const out = animate(node, [from, { opacity: 0, transform: `translateX(${-dir * 24}px)` }], "effects-fast");
  if (!out) { swap(); return; }
  out.finished.then(() => {
    if (swaps.get(node) !== run) return; // уже следующее переключение
    swap();
    sharedAxis(node, dir);
    out.cancel(); // после входящей — иначе fill уходящей оставит узел прозрачным
  }, () => {});
}

// Мягкое появление (скелетон → данные), без сдвига
export function fadeIn(node) {
  const a = animate(node, [{ opacity: 0 }, { opacity: 1 }], "effects-default");
  if (a) a.finished.then(() => a.cancel(), () => {});
}

export function fadeThrough(node) {
  const a = animate(node, [{ opacity: 0, transform: "scale(.98)" }, { opacity: 1, transform: "none" }], "effects-slow");
  if (a) a.finished.then(() => a.cancel(), () => {});
}

// Container transform: View Transitions API, иначе clip-path из прямоугольника карточки
export function containerTransform(fromEl, update, getTarget) {
  const canVT = typeof document.startViewTransition === "function" && !reducedMotion();
  const fromRect = fromEl && fromEl.isConnected ? fromEl.getBoundingClientRect() : null;
  if (canVT && fromEl && fromRect && fromRect.width) {
    fromEl.style.viewTransitionName = "hero-cover";
    let target = null;
    let vt;
    try {
      vt = document.startViewTransition(() => {
        fromEl.style.viewTransitionName = "";
        update();
        target = getTarget && getTarget();
        if (target) target.style.viewTransitionName = "hero-cover";
      });
    } catch {
      fromEl.style.viewTransitionName = "";
      update();
      return;
    }
    vt.finished.finally(() => { if (target) target.style.viewTransitionName = ""; fromEl.style.viewTransitionName = ""; });
    return;
  }
  update();
  const page = getTarget && getTarget("page");
  if (!page || !fromRect || reducedMotion()) { if (page) fadeThrough(page); return; }
  const pr = page.getBoundingClientRect();
  if (!pr.width) return;
  const inset = `inset(${Math.max(0, fromRect.top - pr.top)}px ${Math.max(0, pr.right - fromRect.right)}px ${Math.max(0, pr.bottom - fromRect.bottom)}px ${Math.max(0, fromRect.left - pr.left)}px round 16px)`;
  const a = animate(page, [{ clipPath: inset, opacity: 0.6 }, { clipPath: "inset(0 0 0 0 round 0)", opacity: 1 }], "spatial-default");
  if (a) a.finished.then(() => a.cancel(), () => {});
}
