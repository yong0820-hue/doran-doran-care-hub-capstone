import { useState, useEffect } from 'react'

export default function Header() {
  const [now, setNow] = useState(new Date())

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(timer)
  }, [])

  const timeStr = now.toLocaleTimeString('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
  const dateStr = now.toLocaleDateString('ko-KR', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'short',
  })

  return (
    <header className="header">
      <div className="header-brand">
        <span style={{ fontSize: 28 }}>🏡</span>
        <div>
          <h1>도란도란 케어 허브</h1>
          <div className="header-subtitle">보호자 실시간 모니터링 대시보드</div>
        </div>
      </div>
      <div className="header-right">
        <div className="header-time">{timeStr}</div>
        <div className="header-date">{dateStr}</div>
      </div>
    </header>
  )
}
