import os
import json
import io
from PIL import Image, ImageOps
from deepface import DeepFace

# Configuration Paths
INPUT_PHOTOS_DIR = "./worker_photos"      
OUTPUT_REFS_DIR = "./local_references"    

def prepare_image(image_path: str) -> str:
    """Fixes EXIF orientation and saves a normalized temporary image for DeepFace."""
    with open(image_path, "rb") as f:
        file_bytes = f.read()
        
    img = Image.open(io.BytesIO(file_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    img.thumbnail((800, 800))
    
    temp_path = f"./temp_enroll_{os.path.basename(image_path)}"
    img.save(temp_path, format="JPEG")
    return temp_path

def process_worker_photos():
    os.makedirs(OUTPUT_REFS_DIR, exist_ok=True)
    
    if not os.path.exists(INPUT_PHOTOS_DIR):
        os.makedirs(INPUT_PHOTOS_DIR, exist_ok=True)
        print(f"📁 Created '{INPUT_PHOTOS_DIR}' directory. Please drop worker photos there and rerun.")
        return

    valid_extensions = (".jpg", ".jpeg", ".png")
    photo_files = [f for f in os.listdir(INPUT_PHOTOS_DIR) if f.lower().endswith(valid_extensions)]

    if not photo_files:
        print(f"⚠️ No photos found in '{INPUT_PHOTOS_DIR}'.")
        return

    print(f"🚀 Found {len(photo_files)} photos. Pre-computing facial embeddings...\n")

    success_count = 0
    failure_count = 0

    for filename in photo_files:
        worker_id, _ = os.path.splitext(filename)
        photo_path = os.path.join(INPUT_PHOTOS_DIR, filename)
        json_dest_path = os.path.join(OUTPUT_REFS_DIR, f"{worker_id}.json")
        temp_img_path = None

        print(f"⏳ Processing {filename} (Worker ID: {worker_id})...")

        try:
            # 1. Normalize image
            temp_img_path = prepare_image(photo_path)

            # 2. Extract embedding using the exact same model & backend as your API
            embedding_objs = DeepFace.represent(
                img_path=temp_img_path,
                model_name="VGG-Face",
                detector_backend="mtcnn",
                enforce_detection=True
            )

            # Check for multi-face anomaly in reference photos
            if len(embedding_objs) > 1:
                print(f"❌ Skipped {filename}: Multiple faces detected in reference photo.")
                failure_count += 1
                continue

            worker_embedding = embedding_objs[0]["embedding"]

            # 3. Save as JSON
            with open(json_dest_path, "w") as json_file:
                json.dump(worker_embedding, json_file)

            print(f"✅ Generated: {json_dest_path}")
            success_count += 1

        except ValueError:
            print(f"❌ Skipped {filename}: No clear face detected. Check lighting/pose.")
            failure_count += 1
        except Exception as e:
            print(f"❌ Error processing {filename}: {e}")
            failure_count += 1
        finally:
            if temp_img_path and os.path.exists(temp_img_path):
                os.remove(temp_img_path)

    print("\n" + "=" * 40)
    print(f"🎉 Batch Process Completed!")
    print(f"✔️ Successfully Enrolled: {success_count}")
    print(f"❌ Failed / Skipped:     {failure_count}")
    print("=" * 40)

if __name__ == "__main__":
    process_worker_photos()