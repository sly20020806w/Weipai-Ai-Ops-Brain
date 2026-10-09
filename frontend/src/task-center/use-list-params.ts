import { useSearchParams } from 'react-router-dom';

export function useListParams() {
  const [params, setParams] = useSearchParams();
  const number = Number(params.get('page') ?? 1);
  const page = Number.isSafeInteger(number) && number > 0 && number <= 1000000 ? number : 1;
  function update(key: string, value: string) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value) next.set(key, value); else next.delete(key);
      if (key !== 'page' && key !== 'call_page') next.delete('page');
      return next;
    });
  }
  return { params, page, update };
}
