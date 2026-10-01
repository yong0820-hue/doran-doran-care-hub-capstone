const now = Date.now()
const h = (n) => now - n * 60 * 60 * 1000
const m = (n) => now - n * 60 * 1000

export const mockElder = {
  id: 'elder_001',
  name: '김영자',
  age: 78,
  overallStatus: '양호',
  presenceStatus: 'in',
  presenceLocation: '거실',
  lastSeen: m(26),
}

export const mockDevice = {
  id: 'device_001',
  name: '도란도란 거실 라디오',
  type: 'mock',
  location: '거실',
  status: 'online',
  lastSignal: m(2),
  lastSeen: m(26),
  sensors: {
    radar: {
      presence: true,
      lastUpdated: m(26),
    },
  },
}

export const mockMedication = {
  morning: {
    label: '아침 약',
    scheduledTime: '08:00',
    status: 'taken',
    takenAt: h(6),
  },
  afternoon: {
    label: '점심 약',
    scheduledTime: '12:00',
    status: 'delayed',
    takenAt: null,
  },
  evening: {
    label: '저녁 약',
    scheduledTime: '19:00',
    status: 'pending',
    takenAt: null,
  },
}

export const mockEvents = [
  {
    id: 'evt_001',
    type: 'presence_detected',
    status: 'open',
    deviceId: 'device_001',
    deviceName: '도란도란 거실 라디오',
    location: '거실',
    createdAt: m(26),
    label: '재실 감지됨',
    severity: 'info',
  },
  {
    id: 'evt_002',
    type: 'fall_suspected',
    status: 'open',
    deviceId: 'device_001',
    deviceName: '도란도란 거실 라디오',
    location: '거실',
    createdAt: h(1),
    label: '낙상 의심 감지',
    severity: 'critical',
  },
  {
    id: 'evt_003',
    type: 'medication_delayed',
    status: 'open',
    deviceId: 'device_001',
    deviceName: '도란도란 거실 라디오',
    location: '거실',
    createdAt: h(2),
    label: '약 복용 지연',
    severity: 'warning',
  },
  {
    id: 'evt_004',
    type: 'medication_taken',
    status: 'open',
    deviceId: 'device_001',
    deviceName: '도란도란 거실 라디오',
    location: '거실',
    createdAt: h(6),
    label: '약 복용 완료',
    severity: 'success',
  },
]
