import os
import bz2
import PIL.Image
import numpy as np
import tensorflow as tf
from keras.models import Model
from keras.utils import get_file
from keras.applications.vgg16 import VGG16, preprocess_input
import keras.backend as K

def load_images(images_list, image_size=256):
    loaded_images = list()
    for img_path in images_list:
      img = PIL.Image.open(img_path).convert('RGB').resize((image_size,image_size),PIL.Image.LANCZOS)
      img = np.array(img)
      img = np.expand_dims(img, 0)
      loaded_images.append(img)
    loaded_images = np.vstack(loaded_images)
    return loaded_images

def tf_custom_l1_loss(img1,img2):
  return tf.math.reduce_mean(tf.math.abs(img2-img1), axis=None)

def tf_custom_logcosh_loss(img1,img2):
  return tf.math.reduce_mean(tf.keras.losses.logcosh(img1,img2))

class PerceptualModel:
    def __init__(self, args, batch_size=1, perc_model=None, sess=None):
        self.sess = tf.get_default_session() if sess is None else sess
        K.set_session(self.sess)
        self.epsilon = 0.00000001
        self.lr = args.lr
        self.decay_rate = args.decay_rate
        self.decay_steps = args.decay_steps
        self.img_size = args.image_size
        self.layer = args.use_vgg_layer
        self.vgg_loss = args.use_vgg_loss
        self.face_mask = args.face_mask
        self.use_grabcut = args.use_grabcut
        self.scale_mask = args.scale_mask
        self.mask_dir = args.mask_dir
        if (self.layer <= 0 or self.vgg_loss <= self.epsilon):
            self.vgg_loss = None
        self.pixel_loss = args.use_pixel_loss
        if (self.pixel_loss <= self.epsilon):
            self.pixel_loss = None
        self.mssim_loss = args.use_mssim_loss
        if (self.mssim_loss <= self.epsilon):
            self.mssim_loss = None
        self.lpips_loss = args.use_lpips_loss
        if (self.lpips_loss <= self.epsilon):
            self.lpips_loss = None
        self.l1_penalty = args.use_l1_penalty
        if (self.l1_penalty <= self.epsilon):
            self.l1_penalty = None
        self.batch_size = batch_size
        if perc_model is not None and self.lpips_loss is not None:
            self.perc_model = perc_model
        else:
            self.perc_model = None
        self.ref_img = None
        self.ref_weight = None
        self.perceptual_model = None
        self.ref_img_features = None
        self.features_weight = None
        self.loss = None

    def compare_images(self,img1,img2):
        if self.perc_model is not None:
            return self.perc_model.get_output_for(tf.transpose(img1, perm=[0,3,2,1]), tf.transpose(img2, perm=[0,3,2,1]))
        return 0

    def build_perceptual_model(self, generator):

        # Learning rate
        global_step = tf.Variable(0, dtype=tf.int32, trainable=False, name="global_step")
        incremented_global_step = tf.assign_add(global_step, 1)
        self._reset_global_step = tf.assign(global_step, 0)
        self.learning_rate = tf.train.exponential_decay(self.lr, incremented_global_step,
                self.decay_steps, self.decay_rate, staircase=True)
        self.sess.run([self._reset_global_step])

        generated_image_tensor = generator.generated_image
        generated_image = tf.image.resize_nearest_neighbor(generated_image_tensor,
                                                                  (self.img_size, self.img_size), align_corners=True)

        self.ref_img = tf.get_variable('ref_img', shape=generated_image.shape,
                                                dtype='float32', initializer=tf.initializers.zeros())
        self.ref_weight = tf.get_variable('ref_weight', shape=generated_image.shape,
                                               dtype='float32', initializer=tf.initializers.zeros())

        if (self.vgg_loss is not None):
            vgg16 = VGG16(include_top=False, input_shape=(self.img_size, self.img_size, 3))
            self.perceptual_model = Model(vgg16.input, vgg16.layers[self.layer].output)
            generated_img_features = self.perceptual_model(preprocess_input(self.ref_weight * generated_image))
            self.ref_img_features = tf.get_variable('ref_img_features', shape=generated_img_features.shape,
                                                dtype='float32', initializer=tf.initializers.zeros())
            self.features_weight = tf.get_variable('features_weight', shape=generated_img_features.shape,
                                               dtype='float32', initializer=tf.initializers.zeros())
            self.sess.run([self.features_weight.initializer, self.features_weight.initializer])

        self.loss = 0
        # L1 loss on VGG16 features
        if (self.vgg_loss is not None):
            self.loss += self.vgg_loss * tf_custom_l1_loss(self.features_weight * self.ref_img_features, self.features_weight * generated_img_features)
        # + logcosh loss on image pixels
        if (self.pixel_loss is not None):
            self.loss += self.pixel_loss * tf_custom_logcosh_loss(self.ref_weight * self.ref_img, self.ref_weight * generated_image)
        # + MS-SIM loss on image pixels
        if (self.mssim_loss is not None):
            self.loss += self.mssim_loss * tf.math.reduce_mean(1-tf.image.ssim_multiscale(self.ref_weight * self.ref_img, self.ref_weight * generated_image, 1))
        # + extra perceptual loss on image pixels
        if self.perc_model is not None and self.lpips_loss is not None:
            self.loss += self.lpips_loss * tf.math.reduce_mean(self.compare_images(self.ref_weight * self.ref_img, self.ref_weight * generated_image))
        # + L1 penalty on dlatent weights
        if self.l1_penalty is not None:
            self.loss += self.l1_penalty * 512 * tf.math.reduce_mean(tf.math.abs(generator.dlatent_variable-generator.get_dlatent_avg()))

    def set_reference_images(self, images_list):
        assert(len(images_list) != 0 and len(images_list) <= self.batch_size)
        loaded_image = load_images(images_list, self.img_size)
        image_features = None
        if self.perceptual_model is not None:
            image_features = self.perceptual_model.predict_on_batch(preprocess_input(loaded_image))
            weight_mask = np.ones(self.features_weight.shape)

        if self.face_mask:
            import dlib
            from imutils import face_utils
            import cv2
            detector = dlib.get_frontal_face_detector()

            LANDMARKS_MODEL_URL = 'http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2'

            def unpack_bz2(src_path):
                data = bz2.BZ2File(src_path).read()
                dst_path = src_path[:-4]
                with open(dst_path, 'wb') as fp:
                    fp.write(data)
                return dst_path

            landmarks_model_path = unpack_bz2(get_file('shape_predictor_68_face_landmarks.dat.bz2',
                                                    LANDMARKS_MODEL_URL, cache_subdir='temp'))

            predictor = dlib.shape_predictor(landmarks_model_path)
            image_mask = np.zeros(self.ref_weight.shape)
            for (i, im) in enumerate(loaded_image):
                rects = detector(im, 1)
                # loop over the face detections
                for (j, rect) in enumerate(rects):
                    """
                    Determine the facial landmarks for the face region, then convert the facial landmark (x, y)-coordinates to a NumPy array
                    """
                    shape = predictor(im, rect)
                    shape = face_utils.shape_to_np(shape)

                    # we extract the face
                    vertices = cv2.convexHull(shape)
                    mask = np.zeros(im.shape[:2],np.uint8)
                    cv2.fillConvexPoly(mask, vertices, 1)
                    if self.use_grabcut:
                        bgdModel = np.zeros((1,65),np.float64)
                        fgdModel = np.zeros((1,65),np.float64)
                        rect = (0,0,image_mask.shape[1],image_mask.shape[2])
                        (x,y),radius = cv2.minEnclosingCircle(vertices)
                        center = (int(x),int(y))
                        radius = int(radius*self.scale_mask)
                        mask = cv2.circle(mask,center,radius,cv2.GC_PR_FGD,-1)
                        cv2.fillConvexPoly(mask, vertices, cv2.GC_FGD)
                        cv2.grabCut(im,mask,rect,bgdModel,fgdModel,5,cv2.GC_INIT_WITH_MASK)
                        mask = np.where((mask==2)|(mask==0),0,1)
                    image_mask[i] = np.ones(image_mask[i].shape,np.float32) * np.expand_dims(mask,axis=-1)
                    imask = PIL.Image.fromarray((255*image_mask[i]).astype('uint8'), 'RGB')
                    _, img_name = os.path.split(images_list[i])
                    imask.save(os.path.join(self.mask_dir, f'{img_name}'), 'PNG')
            img = None
        else:
            image_mask = np.ones(self.ref_weight.shape)

        if len(images_list) != self.batch_size:
            if image_features is not None:
                features_space = list(self.features_weight.shape[1:])
                existing_features_shape = [len(images_list)] + features_space
                empty_features_shape = [self.batch_size - len(images_list)] + features_space
                existing_examples = np.ones(shape=existing_features_shape)
                empty_examples = np.zeros(shape=empty_features_shape)
                weight_mask = np.vstack([existing_examples, empty_examples])
                image_features = np.vstack([image_features, np.zeros(empty_features_shape)])

            images_space = list(self.ref_weight.shape[1:])
            existing_images_space = [len(images_list)] + images_space
            empty_images_space = [self.batch_size - len(images_list)] + images_space
            existing_images = np.ones(shape=existing_images_space)
            empty_images = np.zeros(shape=empty_images_space)
            image_mask = image_mask * np.vstack([existing_images, empty_images])
            loaded_image = np.vstack([loaded_image, np.zeros(empty_images_space)])

        if image_features is not None:
            self.sess.run(tf.assign(self.features_weight, weight_mask))
            self.sess.run(tf.assign(self.ref_img_features, image_features))
        self.sess.run(tf.assign(self.ref_weight, image_mask))
        self.sess.run(tf.assign(self.ref_img, loaded_image))

    def optimize(self, vars_to_optimize, iterations=200):
        vars_to_optimize = vars_to_optimize if isinstance(vars_to_optimize, list) else [vars_to_optimize]
        optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        min_op = optimizer.minimize(self.loss, var_list=[vars_to_optimize])
        self.sess.run(tf.variables_initializer(optimizer.variables()))
        self.sess.run(self._reset_global_step)
        fetch_ops = [min_op, self.loss, self.learning_rate]
        for _ in range(iterations):
            _, loss, lr = self.sess.run(fetch_ops)
            yield {"loss":loss, "lr": lr}
