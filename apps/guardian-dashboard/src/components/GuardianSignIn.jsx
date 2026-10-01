import { useState } from 'react'

export default function GuardianSignIn({ state, onLogin, onLogout }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const busy = ['initializing', 'authorizing', 'signing_in', 'signing_out'].includes(state.status)
  const submit = async (event) => {
    event.preventDefault()
    const supplied = password
    setPassword('')
    await onLogin(email.trim(), supplied)
  }
  return <main className="main-content">
    <section className="card" aria-labelledby="guardian-login-title" style={{ maxWidth: 480, margin: '24px auto' }}>
      <h2 id="guardian-login-title">보호자 로그인</h2>
      <p>테스트 프로젝트에서 관리자가 연결한 보호자 계정으로 로그인해 주세요.</p>
      {busy && <p role="status">{state.status === 'signing_in' ? '로그인 중…' : state.status === 'signing_out' ? '로그아웃 중…' : '인증과 접근 권한을 확인하고 있습니다…'}</p>}
      {state.error && <p className="medication-action-error" role="alert">{state.error}</p>}
      {state.status !== 'no_access' && <form onSubmit={submit}>
        <label>이메일<input type="email" autoComplete="username" required value={email}
          disabled={busy} onChange={(event) => setEmail(event.target.value)} /></label>
        <label>비밀번호<input type="password" autoComplete="current-password" required value={password}
          disabled={busy} onChange={(event) => setPassword(event.target.value)} /></label>
        <button type="submit" className="resolve-btn" disabled={busy}>로그인</button>
      </form>}
      {(state.uid || state.status === 'error') && <button type="button" className="resolve-btn" disabled={busy} onClick={onLogout}>로그아웃</button>}
    </section>
  </main>
}
