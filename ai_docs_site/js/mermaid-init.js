(() => {
  const blocks = document.querySelectorAll('pre.mermaid > code, code.language-mermaid');
  blocks.forEach((code) => {
    const pre = code.closest('pre');
    const parent = pre ? pre.parentElement : code.parentElement;
    if (!parent) return;
    const div = document.createElement('div');
    div.className = 'mermaid';
    div.textContent = code.textContent || '';
    if (pre) {
      parent.replaceChild(div, pre);
    } else {
      parent.replaceChild(div, code);
    }
  });
  if (window.mermaid) {
    mermaid.initialize({ startOnLoad: true });
  }
})();
