/** @type {import('tailwindcss').Config} */
// Phase 6A: theme tokens are defined as CSS variables in
// `src/index.css` (see `:root` for dark, `.theme-light` for light).
// Tailwind reads those variables via `theme.extend.colors` so
// components consume semantic classes like `bg-surface`,
// `text-fg-primary`, `border-border` and never hardcoded hex values.
//
// Do not add raw hex values anywhere outside this file or
// `index.css`. The verification script greps the codebase to
// enforce this invariant on major surfaces.
export default {
  darkMode: ['selector', '[data-theme="dark"]'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // --- Canvas / surfaces ---
        bg: 'var(--bg)',
        'bg-elevated': 'var(--bg-elevated)',
        surface: 'var(--surface)',
        'surface-2': 'var(--surface-2)',
        'surface-hover': 'var(--surface-hover)',
        // --- Borders ---
        border: 'var(--border)',
        'border-strong': 'var(--border-strong)',
        // --- Text ---
        'fg-primary': 'var(--fg-primary)',
        'fg-secondary': 'var(--fg-secondary)',
        'fg-muted': 'var(--fg-muted)',
        'fg-inverse': 'var(--fg-inverse)',
        // --- Brand / primary ---
        primary: 'var(--primary)',
        'primary-hover': 'var(--primary-hover)',
        'primary-soft': 'var(--primary-soft)',
        'primary-foreground': 'var(--primary-foreground)',
        // --- State ---
        success: 'var(--success)',
        'success-soft': 'var(--success-soft)',
        warning: 'var(--warning)',
        'warning-soft': 'var(--warning-soft)',
        danger: 'var(--danger)',
        'danger-soft': 'var(--danger-soft)',
        info: 'var(--info)',
        'info-soft': 'var(--info-soft)',
        ai: 'var(--ai-accent)',
        'ai-soft': 'var(--ai-soft)',
      },
      fontFamily: {
        // Compact enterprise sans-serif stack.  Falls back to the
        // platform's system UI font when no web font is loaded —
        // intentional, keeps the bundle small and prevents FOUT.
        sans: [
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'Menlo',
          'Monaco',
          'Consolas',
          'Liberation Mono',
          'monospace',
        ],
      },
      boxShadow: {
        'card-sm': 'var(--shadow-card-sm)',
        'card-md': 'var(--shadow-card-md)',
        focus: 'var(--shadow-focus)',
      },
      borderRadius: {
        DEFAULT: '0.375rem',
      },
    },
  },
  plugins: [],
}
