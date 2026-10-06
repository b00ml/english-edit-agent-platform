import type { ReactNode } from 'react'

const shapes: Record<string, ReactNode> = {
  create: <><path d="m12 3 1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5Z"/><path d="m20 15 .8 2.2L23 18l-2.2.8L20 21l-.8-2.2L17 18l2.2-.8Z"/></>,
  tasks: <><rect x="4" y="4" width="16" height="17" rx="2"/><path d="M9 4V2h6v2M8 10h8M8 14h8M8 18h5"/></>,
  quality: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/></>,
  library: <><path d="M4 5h6l2 2h8v13H4Z"/><path d="M8 12h8M8 16h5"/></>,
  book: <><path d="M12 6v15M12 6C9 3 4 4 3 5v14c4-2 7-1 9 2 2-3 5-4 9-2V5c-4-2-7-1-9 1Z"/></>,
  samples: <><rect x="4" y="7" width="13" height="14" rx="2"/><path d="M8 3h12v14M8 12h5M8 16h5"/></>,
  dashboard: <><rect x="3" y="3" width="7" height="8" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="3" y="15" width="7" height="6" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/></>,
  costs: <><rect x="3" y="5" width="18" height="15" rx="2"/><path d="M3 10h18M7 16h3M6 5V3h12"/></>,
  trace: <><circle cx="5" cy="5" r="2"/><circle cx="19" cy="12" r="2"/><circle cx="5" cy="19" r="2"/><path d="M7 5h4a4 4 0 0 1 4 4v3h2M15 12v3a4 4 0 0 1-4 4H7"/></>,
  bell: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></>,
  settings: <><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="10" cy="18" r="2"/></>,
  users: <><circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M17 4a3 3 0 0 1 0 6M19 21v-3a5 5 0 0 0-3-4"/></>,
  menu: <path d="M4 6h16M4 12h16M4 18h16"/>,
  close: <path d="m6 6 12 12M18 6 6 18"/>,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6"/>,
  refresh: <><path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/></>,
  chevron: <path d="m9 5 7 7-7 7"/>,
  logout: <><path d="M9 4H4v16h5M9 12h12m-5-5 5 5-5 5"/></>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  copy: <><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/></>,
  search: <><circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/></>,
}

export default function Icon({ name, size = 18, className = '' }: { name: string; size?: number; className?: string }) {
  return <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{shapes[name] ?? shapes.info}</svg>
}
