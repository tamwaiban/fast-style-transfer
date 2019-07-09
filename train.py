import os
import logging
from argparse import ArgumentParser

import tensorflow as tf
import tensorflow_datasets as tfds

from networks import StyleContentModel, TransformerNet

logging.basicConfig(level=logging.INFO)
AUTOTUNE = tf.data.experimental.AUTOTUNE


def load_img(path_to_img):
    img = tf.io.read_file(path_to_img)
    img = tf.image.decode_image(img, channels=3)
    img = tf.cast(img, tf.float32)
    img = img[tf.newaxis, :]
    return img


def style_content_loss(outputs, transformed_outputs):
    transformed_style_outputs = transformed_outputs["style"]
    content_outputs = outputs["content"]
    transformed_content_outputs = transformed_outputs["content"]

    style_loss = tf.add_n(
        [
            tf.reduce_mean(
                (transformed_style_outputs[name] - style_targets[name]) ** 2
            )
            for name in transformed_style_outputs.keys()
        ]
    )

    content_loss = tf.add_n(
        [
            tf.reduce_mean(
                (transformed_content_outputs[name] - content_outputs[name])
                ** 2
            )
            for name in content_outputs.keys()
        ]
    )
    return style_loss, content_loss


def high_pass_x_y(image):
    x_var = image[:, :, 1:, :] - image[:, :, :-1, :]
    y_var = image[:, 1:, :, :] - image[:, :-1, :, :]

    return x_var, y_var


def total_variation_loss(image):
    x_deltas, y_deltas = high_pass_x_y(image)
    return tf.reduce_mean(x_deltas ** 2) + tf.reduce_mean(y_deltas ** 2)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--log-dir", default="logs/style")
    parser.add_argument("--learning-rate", default=1e-3, type=float)
    parser.add_argument("--image-size", default=256, type=int)
    parser.add_argument("--batch-size", default=4, type=int)
    parser.add_argument("--epochs", default=2, type=int)
    parser.add_argument("--content-weight", default=1e4, type=float)
    parser.add_argument("--style-weight", default=1e-2, type=float)
    parser.add_argument("--tv-weight", default=1, type=float)
    args = parser.parse_args()

    style_path = tf.keras.utils.get_file(
        "kandinsky.jpg",
        "https://storage.googleapis.com/download.tensorflow.org/example_images/Vassily_Kandinsky%2C_1913_-_Composition_7.jpg",
    )
    style_image = load_img(style_path)
    test_content_path = tf.keras.utils.get_file(
        "turtle.jpg",
        "https://storage.googleapis.com/download.tensorflow.org/example_images/Green_Sea_Turtle_grazing_seagrass.jpg",
    )
    test_content_image = load_img(test_content_path)

    content_layers = ["block5_conv2"]
    style_layers = [
        "block1_conv1",
        "block2_conv1",
        "block3_conv1",
        "block4_conv1",
        "block5_conv1",
    ]

    num_content_layers = len(content_layers)
    num_style_layers = len(style_layers)

    extractor = StyleContentModel(style_layers, content_layers)
    transformer = TransformerNet()

    # Precompute style_targets
    style_targets = extractor(style_image)["style"]

    optimizer = tf.optimizers.Adam(
        learning_rate=args.learning_rate, beta_1=0.99, epsilon=1e-1
    )

    ckpt = tf.train.Checkpoint(
        step=tf.Variable(1), optimizer=optimizer, transformer=transformer
    )
    manager = tf.train.CheckpointManager(ckpt, args.log_dir, max_to_keep=3)
    ckpt.restore(manager.latest_checkpoint)
    if manager.latest_checkpoint:
        print("Restored from {}".format(manager.latest_checkpoint))
    else:
        print("Initializing from scratch.")

    train_loss = tf.keras.metrics.Mean(name="train_loss")
    train_style_loss = tf.keras.metrics.Mean(name="train_style_loss")
    train_content_loss = tf.keras.metrics.Mean(name="train_content_loss")
    train_tv_loss = tf.keras.metrics.Mean(name="train_tv_loss")

    train_summary_writer = tf.summary.create_file_writer(
        os.path.join(args.log_dir, "train")
    )

    @tf.function()
    def train_step(image):
        with tf.GradientTape() as tape:

            transformed_image = transformer(image)

            outputs = extractor(image)
            transformed_outputs = extractor(transformed_image)

            style_loss, content_loss = style_content_loss(
                outputs, transformed_outputs
            )
            va_loss = args.tv_weight * total_variation_loss(image)
            style_loss *= args.style_weight / num_style_layers
            content_loss *= args.content_weight / num_content_layers

            loss = style_loss + content_loss + va_loss

        gradients = tape.gradient(loss, transformer.trainable_variables)
        optimizer.apply_gradients(
            zip(gradients, transformer.trainable_variables)
        )

        # Log the losses
        train_loss(loss)
        train_style_loss(style_loss)
        train_content_loss(content_loss)
        train_tv_loss(va_loss)

    def _crop(features):
        image = tf.image.resize_with_crop_or_pad(
            features["image"], args.image_size, args.image_size
        )
        image = tf.cast(image, tf.float32)
        return image

    # Warning: Downloads the full coco2014 dataset
    ds = tfds.load(
        "coco2014", split=tfds.Split.TRAIN, data_dir="~/tensorflow_datasets"
    )
    ds = ds.map(_crop).shuffle(1000).batch(args.batch_size).prefetch(AUTOTUNE)

    for _ in range(args.epochs):
        for image in ds:
            train_step(image)

            ckpt.step.assign_add(1)
            step = int(ckpt.step)

            if step % 500 == 0:
                with train_summary_writer.as_default():
                    tf.summary.scalar("loss", train_loss.result(), step=step)
                    tf.summary.scalar(
                        "style_loss", train_style_loss.result(), step=step
                    )
                    tf.summary.scalar(
                        "content_loss", train_content_loss.result(), step=step
                    )
                    tf.summary.scalar(
                        "tv_loss", train_tv_loss.result(), step=step
                    )
                    tf.summary.image(
                        "Content Image", test_content_image / 255.0, step=step
                    )
                    tf.summary.image(
                        "Style Image", style_image / 255.0, step=step
                    )
                    test_styled_image = transformer(test_content_image)
                    tf.summary.image(
                        "Styled Image", test_styled_image / 255.0, step=step
                    )

                template = "Step {}, Loss: {}, Style Loss: {}, Content Loss: {}, TV Loss: {}"
                print(
                    template.format(
                        step,
                        train_loss.result(),
                        train_style_loss.result(),
                        train_content_loss.result(),
                        train_tv_loss.result(),
                    )
                )
                save_path = manager.save()
                print(
                    "Saved checkpoint for step {}: {}".format(
                        int(ckpt.step), save_path
                    )
                )

            train_loss.reset_states()
            train_style_loss.reset_states()
            train_content_loss.reset_states()
            train_tv_loss.reset_states()
