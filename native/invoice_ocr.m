#import <Foundation/Foundation.h>
#import <Vision/Vision.h>
#import <ImageIO/ImageIO.h>

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSError *error = nil;
        VNRecognizeTextRequest *request = [VNRecognizeTextRequest new];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.usesLanguageCorrection = NO;
        NSArray *supported = [request supportedRecognitionLanguagesAndReturnError:&error];
        if (argc < 2) {
            puts([[supported componentsJoinedByString:@","] UTF8String]);
            return 0;
        }
        NSMutableArray *languages = [NSMutableArray new];
        for (NSString *lang in @[@"ru-RU", @"en-US", @"ko-KR"]) {
            if ([supported containsObject:lang]) [languages addObject:lang];
        }
        if (argc > 2) {
            languages = [NSMutableArray arrayWithArray:[@(argv[2]) componentsSeparatedByString:@","]];
        }
        request.recognitionLanguages = languages;
        request.minimumTextHeight = 0.002;
        CGImageSourceRef source = CGImageSourceCreateWithURL((__bridge CFURLRef)[NSURL fileURLWithPath:@(argv[1])], NULL);
        if (!source) return 3;
        CGImageRef image = CGImageSourceCreateThumbnailAtIndex(source, 0, (__bridge CFDictionaryRef)@{
            (id)kCGImageSourceCreateThumbnailFromImageAlways: @YES,
            (id)kCGImageSourceCreateThumbnailWithTransform: @YES,
            (id)kCGImageSourceThumbnailMaxPixelSize: @4096});
        CFRelease(source);
        if (!image) return 3;
        size_t width = CGImageGetWidth(image), height = CGImageGetHeight(image);
        CGColorSpaceRef space = CGColorSpaceCreateDeviceRGB();
        CGContextRef pixels = CGBitmapContextCreate(NULL, width, height, 8, width * 4, space, kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big);
        CGContextDrawImage(pixels, CGRectMake(0, 0, width, height), image);
        unsigned char *bytes = CGBitmapContextGetData(pixels);
        // Phone screenshots have black bands; they waste Vision's image resolution.
        // Keep the longest substantial content run, ignoring the small home indicator.
        size_t first = height, last = 0, runStart = 0, runLength = 0, bestLength = 0;
        for (size_t y=0; y<height; y++) {
            size_t visible=0, samples=0;
            for (size_t x=0; x<width; x+=4) {
                size_t i=(y * width + x) * 4;
                if (MAX(bytes[i], MAX(bytes[i+1],bytes[i+2]))>45) visible++;
                samples++;
            }
            if (visible > samples * 0.35) {
                if (!runLength) runStart=y;
                runLength++;
                if (runLength>bestLength) { bestLength=runLength; first=runStart; last=y; }
            } else runLength=0;
        }
        CGContextRelease(pixels);
        if (last>first && last-first > height * 0.15 && last-first < height * 0.98) {
            size_t top=first>5 ? first-5 : 0;
            CGImageRef cropped=CGImageCreateWithImageInRect(image, CGRectMake(0, top, width, MIN(height-top, last-top+6)));
            if(cropped) { CGImageRelease(image); image=cropped; }
        }
        width=CGImageGetWidth(image); height=CGImageGetHeight(image);
        if(width<2400) {
            size_t targetWidth=2400, targetHeight=height*targetWidth/width;
            CGContextRef enlarged=CGBitmapContextCreate(NULL,targetWidth,targetHeight,8,targetWidth*4,space,kCGImageAlphaPremultipliedLast);
            CGContextSetInterpolationQuality(enlarged,kCGInterpolationHigh);
            CGContextDrawImage(enlarged,CGRectMake(0,0,targetWidth,targetHeight),image);
            CGImageRef scaled=CGBitmapContextCreateImage(enlarged);
            CGContextRelease(enlarged); CGImageRelease(image); image=scaled;
        }
        CGColorSpaceRelease(space);
        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithCGImage:image options:@{}];
        if (![handler performRequests:@[request] error:&error]) {
            fprintf(stderr, "%s\n", [[error localizedDescription] UTF8String]);
            return 1;
        }
        NSMutableArray *output = [NSMutableArray new];
        for (VNRecognizedTextObservation *observation in request.results) {
            VNRecognizedText *text = [[observation topCandidates:1] firstObject];
            CGRect box = observation.boundingBox;
            if (text) [output addObject:@{@"text": text.string, @"confidence": @(text.confidence),
                @"x": @(box.origin.x), @"y": @(box.origin.y),
                @"w": @(box.size.width), @"h": @(box.size.height)}];
        }
        NSData *data = [NSJSONSerialization dataWithJSONObject:output options:NSJSONWritingSortedKeys error:&error];
        if (!data) return 2;
        [[NSFileHandle fileHandleWithStandardOutput] writeData:data];
        CGImageRelease(image);
    }
    return 0;
}
