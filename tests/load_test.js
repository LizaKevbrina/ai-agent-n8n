import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Rate, Trend } from 'k6/metrics';

// Custom metrics
const errorRate = new Rate('errors');
const validationDuration = new Trend('validation_duration');
const ragDuration = new Trend('rag_duration');
const llmDuration = new Trend('llm_duration');
const e2eDuration = new Trend('e2e_duration');

// Test configuration
export let options = {
  stages: [
    { duration: '2m', target: 50 },   // Ramp-up to 50 users
    { duration: '5m', target: 50 },   // Stay at 50 users
    { duration: '2m', target: 100 },  // Ramp-up to 100 users
    { duration: '5m', target: 100 },  // Stay at 100 users
    { duration: '2m', target: 0 },    // Ramp-down
  ],
  thresholds: {
    'http_req_duration': ['p(95)<2000'],      // 95% < 2s
    'http_req_failed': ['rate<0.1'],          // < 10% errors
    'errors': ['rate<0.05'],                  // < 5% custom errors
    'e2e_duration': ['p(95)<3000'],           // E2E < 3s
    'llm_duration': ['p(95)<1500'],           // LLM < 1.5s
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost';

// Sample questions
const questions = [
  'Какие квартиры есть в ЖК Солнечный?',
  'Покажи 2-комнатные до 6 млн',
  'Какая площадь у квартир?',
  'Какая погода будет завтра?',
  'Расскажи про район',
  'Есть ли парковка?',
  'Когда сдача дома?',
  'Можно ли в ипотеку?',
];

export default function () {
  const sessionId = `load_test_${__VU}_${__ITER}`;
  const question = questions[Math.floor(Math.random() * questions.length)];
  const correlationId = `${Date.now()}-${sessionId}`;
  
  const e2eStart = Date.now();
  
  group('Full Pipeline', function () {
    // 1. Validation
    group('Validation Service', function () {
      const payload = JSON.stringify({
        chat_input: question,
        session_id: sessionId,
      });
      
      const validationStart = Date.now();
      const validationRes = http.post(
        `${BASE_URL}:8001/api/v1/validate`,
        payload,
        {
          headers: {
            'Content-Type': 'application/json',
            'X-Correlation-ID': correlationId,
          },
        }
      );
      validationDuration.add(Date.now() - validationStart);
      
      const validationCheck = check(validationRes, {
        'validation: status 200': (r) => r.status === 200,
        'validation: is_valid true': (r) => JSON.parse(r.body).is_valid === true,
        'validation: < 500ms': (r) => r.timings.duration < 500,
      });
      
      if (!validationCheck) {
        errorRate.add(1);
        return;
      }
    });
    
    // 2. Intent Classification
    group('Intent Classifier', function () {
      const payload = JSON.stringify({
        text: question,
        session_id: sessionId,
      });
      
      const intentRes = http.post(
        `${BASE_URL}:8004/api/v1/classify`,
        payload,
        {
          headers: {
            'Content-Type': 'application/json',
            'X-Correlation-ID': correlationId,
          },
        }
      );
      
      const intentCheck = check(intentRes, {
        'intent: status 200': (r) => r.status === 200,
        'intent: has classification': (r) => 'is_real_estate' in JSON.parse(r.body),
      });
      
      if (!intentCheck) {
        errorRate.add(1);
        return;
      }
      
      const intentData = JSON.parse(intentRes.body);
      const isRealEstate = intentData.is_real_estate;
      
      // 3. RAG (if real estate)
      if (isRealEstate) {
        group('RAG Service', function () {
          const ragPayload = JSON.stringify({
            question: question,
            session_id: sessionId,
            match_count: 5,
          });
          
          const ragStart = Date.now();
          const ragRes = http.post(
            `${BASE_URL}:8003/api/v1/search`,
            ragPayload,
            {
              headers: {
                'Content-Type': 'application/json',
                'X-Correlation-ID': correlationId,
              },
            }
          );
          ragDuration.add(Date.now() - ragStart);
          
          check(ragRes, {
            'rag: status 200': (r) => r.status === 200,
            'rag: has context': (r) => 'context' in JSON.parse(r.body),
            'rag: < 1s': (r) => r.timings.duration < 1000,
          });
        });
      }
    });
    
    // 4. Memory Get
    group('Memory Service Get', function () {
      const memoryPayload = JSON.stringify({
        session_id: sessionId,
        prompt_version: 'v1',
      });
      
      const memoryRes = http.post(
        `${BASE_URL}:8006/api/v1/get`,
        memoryPayload,
        {
          headers: {
            'Content-Type': 'application/json',
            'X-Correlation-ID': correlationId,
          },
        }
      );
      
      check(memoryRes, {
        'memory get: status 200': (r) => r.status === 200,
        'memory get: has history': (r) => 'history' in JSON.parse(r.body),
      });
    });
    
    // 5. LLM Generation (simulated - expensive)
    group('LLM Service', function () {
      const llmPayload = JSON.stringify({
        messages: [],
        system_prompt: 'You are a helpful assistant.',
        context: '',
        rag_used: false,
        question: question,
        session_id: sessionId,
        temperature: 0.7,
        max_tokens: 100,
        prompt_version: 'v1',
      });
      
      const llmStart = Date.now();
      const llmRes = http.post(
        `${BASE_URL}:8005/api/v1/generate`,
        llmPayload,
        {
          headers: {
            'Content-Type': 'application/json',
            'X-Correlation-ID': correlationId,
          },
          timeout: '60s',
        }
      );
      llmDuration.add(Date.now() - llmStart);
      
      const llmCheck = check(llmRes, {
        'llm: status 200': (r) => r.status === 200,
        'llm: has text': (r) => JSON.parse(r.body).text?.length > 0,
        'llm: < 2s': (r) => r.timings.duration < 2000,
      });
      
      if (!llmCheck) {
        errorRate.add(1);
      }
    });
  });
  
  e2eDuration.add(Date.now() - e2eStart);
  
  // Think time between requests
  sleep(Math.random() * 3 + 1);  // 1-4 seconds
}

export function handleSummary(data) {
  return {
    'load_test_results.json': JSON.stringify(data),
    stdout: textSummary(data, { indent: ' ', enableColors: true }),
  };
}
