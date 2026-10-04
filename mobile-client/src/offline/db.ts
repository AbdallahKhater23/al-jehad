export const OFFLINE_DB_NAME = 'site_attendance_offline';
export const OFFLINE_DB_VERSION = 1;

export type ObjectStoreName = 'meta' | 'punches' | 'photos';

function idbRequest<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error('IndexedDB request failed'));
  });
}

let opening: Promise<IDBDatabase> | null = null;

export function openDb(): Promise<IDBDatabase> {
  if (opening) return opening;
  opening = new Promise<IDBDatabase>((resolve, reject) => {
    const req = indexedDB.open(OFFLINE_DB_NAME, OFFLINE_DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('meta')) {
        db.createObjectStore('meta', { keyPath: 'key' });
      }
      if (!db.objectStoreNames.contains('punches')) {
        const s = db.createObjectStore('punches', { keyPath: 'client_punch_id' });
        s.createIndex('by_worker_status', ['worker_id', 'status']);
        s.createIndex('by_worker', 'worker_id');
      }
      if (!db.objectStoreNames.contains('photos')) {
        db.createObjectStore('photos', { keyPath: 'client_punch_id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error('IndexedDB unavailable'));
  });
  opening.catch(() => {
    opening = null;
  });
  return opening;
}

export async function dbGet<T>(store: ObjectStoreName, key: IDBValidKey): Promise<T | undefined> {
  const db = await openDb();
  return idbRequest<T | undefined>(db.transaction(store, 'readonly').objectStore(store).get(key));
}

export async function dbPut<T>(store: ObjectStoreName, value: T): Promise<void> {
  const db = await openDb();
  await idbRequest(db.transaction(store, 'readwrite').objectStore(store).put(value));
}

export async function dbDelete(store: ObjectStoreName, key: IDBValidKey): Promise<void> {
  const db = await openDb();
  await idbRequest(db.transaction(store, 'readwrite').objectStore(store).delete(key));
}

export async function dbGetAll<T>(store: ObjectStoreName): Promise<T[]> {
  const db = await openDb();
  return idbRequest<T[]>(db.transaction(store, 'readonly').objectStore(store).getAll());
}

export async function dbGetAllByIndex<T>(
  store: ObjectStoreName,
  indexName: string,
  range?: IDBKeyRange | IDBValidKey,
): Promise<T[]> {
  const db = await openDb();
  const idx = db.transaction(store, 'readonly').objectStore(store).index(indexName);
  if (range !== undefined) {
    return idbRequest<T[]>(idx.getAll(range as IDBKeyRange));
  }
  return idbRequest<T[]>(idx.getAll());
}

export function resetDbCache(): void {
  opening = null;
}
